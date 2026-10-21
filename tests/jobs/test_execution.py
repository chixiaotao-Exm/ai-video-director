"""Historical jobs never borrow a model from today's settings."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.config import LLMRoute, Settings
from avd.jobs.execution import execution_data, record_request, route_snapshot
from avd.jobs.models import Job, JobStatus
from avd.llm import FakeProvider, LLMRouter
from avd.runtime import Runtime
from avd.state.models import Project


def test_legacy_queue_marker_is_not_actual_request_evidence() -> None:
    job = Job(
        job_type="director_report",
        payload={},
        model_key="old-claude",
        attempts=11,
        status=JobStatus.FAILED,
    )
    assert execution_data(job)["source"] == "queue_marker"
    assert execution_data(job)["provider"] is None
    assert execution_data(job)["model"] == "old-claude"


def test_multiple_steps_and_new_retry_keep_old_attempt_evidence() -> None:
    job = Job(
        job_type="analyze_video",
        payload={},
        model_key="queue-model",
        attempts=1,
        status=JobStatus.RUNNING,
    )
    record_request(job, "whisper", LLMRoute(provider="local", model="small"))
    record_request(job, "analysis", LLMRoute(provider="gemini", model="gemini-test"))
    record_request(job, "analysis", LLMRoute(provider="gemini", model="gemini-test"))
    view = execution_data(job)
    assert [item["role"] for item in view["steps"]] == ["whisper", "analysis"]
    assert view["steps"][1]["calls"] == 2
    job.status = JobStatus.QUEUED
    job.payload = {
        **job.payload,
        "llm_route": {"role": "analysis", "provider": "openai", "model": "new-gpt"},
    }
    view = execution_data(job)
    assert view["source"] == "snapshot"
    assert view["model"] == "new-gpt"
    assert len(view["history"]) == 2
    job.status, job.attempts = JobStatus.RUNNING, 2
    assert execution_data(job)["source"] == "snapshot"
    record_request(job, "analysis", LLMRoute(provider="openai", model="new-gpt"))
    assert execution_data(job)["source"] == "actual"
    assert len(execution_data(job)["steps"]) == 1
    assert len(execution_data(job)["history"]) == 3


def test_router_observes_the_overridden_route_even_when_provider_fails() -> None:
    seen: list[tuple[str, LLMRoute]] = []
    provider = FakeProvider([RuntimeError("HTTP 429 QUOTA_EXHAUSTED")])
    route = LLMRoute(provider="openai", model="captured-gpt")
    router = LLMRouter(
        Settings(_env_file=None),
        providers={"openai": provider},
        route_overrides={"director": route},
        request_observer=lambda role, current: seen.append((role, current)),
    )
    with pytest.raises(RuntimeError, match="QUOTA_EXHAUSTED"):
        router.chat("director", [{"role": "user", "content": "report"}])
    assert provider.calls[0]["model"] == "captured-gpt"
    assert seen == [("director", route)]


def test_director_failure_route_is_persisted_and_manual_retry_selects_current_route(
    tmp_path: Path,
) -> None:
    old = FakeProvider([RuntimeError("HTTP 429 QUOTA_EXHAUSTED")])
    new = FakeProvider(["任务已检查。"])
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        director_provider="openai",
        director_model="new-gpt",
    )
    runtime = Runtime(settings, providers={"anthropic": old, "openai": new})
    try:
        with runtime.session_factory() as session:
            project = Project(title="route regression", budget_limit=None)
            session.add(project)
            session.commit()
            project_id = project.id
        job_id = runtime.queue.submit(
            "api",
            "director_report",
            {
                "text": "[系统通知] 检查任务",
                "llm_route": {"role": "director", "provider": "anthropic", "model": "old-claude"},
            },
            model_key="old-claude",
            project_id=project_id,
        )
        job = runtime.queue.claim_next("api")
        assert job is not None
        handler = runtime.build_registry().get("director_report")
        assert handler is not None
        with pytest.raises(RuntimeError, match="QUOTA_EXHAUSTED"):
            handler(job)
        runtime.queue.fail(job_id, "HTTP 429 QUOTA_EXHAUSTED", retry=False)
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            global_job = client.get("/jobs").json()[0]
            project_job = client.get(f"/projects/{project_id}/jobs").json()[0]
            for view in (global_job, project_job):
                assert view["execution"]["model"] == "old-claude"
                assert view["execution"]["source"] == "actual"
                assert view["failure"]["code"] == "quota_exhausted"
                assert view["failure"]["retryable"] is False
            retry = client.post(f"/jobs/{job_id}/retry")
            assert retry.status_code == 202, retry.text
            assert retry.json()["execution"]["model"] == "new-gpt"
            assert retry.json()["execution"]["source"] == "snapshot"
            assert len(retry.json()["execution"]["history"]) == 1
            assert runtime.queue.get(job_id).payload["llm_route"] == route_snapshot(
                settings, "director"
            )
            retry_job = runtime.queue.claim_next("api")
            assert retry_job is not None
            handler(retry_job)
            saved = runtime.queue.get(job_id)
            assert execution_data(saved)["model"] == "new-gpt"
            assert execution_data(saved)["source"] == "actual"
            assert old.calls[0]["model"] == "old-claude"
            assert len(old.calls) == 1
            assert new.calls[0]["model"] == "new-gpt"
            from avd.director.agent import DirectorAgent

            events = [
                item
                for item in DirectorAgent(runtime).history(project_id)
                if item.get("event_id") == job_id and item["role"] == "user"
            ]
            assert len(events) == 1
    finally:
        runtime.close()
