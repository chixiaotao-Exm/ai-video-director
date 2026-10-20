"""Browser workspace actions preserve generation gates and never contact providers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from avd.api.app import create_app
from avd.api.workspace import install_workspace_routes, redact_public
from avd.config import Settings
from avd.director.review import Review, approve, approve_budget, review_status
from avd.jobs.models import Job, JobStatus
from avd.llm.ledger import CostEntry, CostLedger
from avd.llm.providers.fake import FakeProvider
from avd.llm.reservations import reserved_cost
from avd.runtime import Runtime
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def workspace(tmp_path: Path):
    provider = FakeProvider([])
    runtime = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            openai_api_key="secret-provider-key",
            job_estimated_costs={"image_gen": 0.2},
        ),
        providers={"openai": provider, "anthropic": provider, "gemini": provider},
    )
    application = create_app(runtime=runtime)
    if not any(getattr(route, "path", None) == "/dashboard" for route in application.routes):
        install_workspace_routes(application, lambda: runtime)
    try:
        with TestClient(application, base_url="http://127.0.0.1") as client:
            yield client, runtime, application
    finally:
        runtime.close()
    assert not provider.calls


def scene(
    runtime: Runtime, *, count: int = 2
) -> tuple[str, dict[tuple[NodeKind, int | None], str]]:
    with runtime.session_factory() as session:
        repo = ProjectRepo(session)
        project = repo.create_project("工作台演示", source_video_path="source.mp4", budget_limit=10)
        character = Character(
            name="Actor",
            reference_images={},
            fixed_appearance_prompt="actor",
            consent_confirmed=True,
        )
        voice = Voice(name="Voice", sample_audio_paths=[], engine="fake", consent_confirmed=True)
        session.add_all([character, voice])
        session.flush()
        repo.attach_assets(project, character, voice)
        analysis = repo.add_node(project, NodeKind.ANALYSIS)
        repo.record_version(analysis, {})
        graph = repo.create_shot_graph(project, count)
        repo.record_version(graph["script"], {})
        for kind in (
            NodeKind.SHOT_FRAME,
            NodeKind.SHOT_VIDEO,
            NodeKind.VOICEOVER,
            NodeKind.LIPSYNC,
            NodeKind.SUBTITLES,
            NodeKind.FINAL,
        ):
            for node in [item for item in project.nodes if item.kind == kind]:
                extension = ".png" if kind == NodeKind.SHOT_FRAME else ".mp4"
                path = f"projects/{project.id}/{node.id}/original{extension}"
                target = runtime.settings.data_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"offline media fixture")
                node.recipe = runtime.engine._effective_recipe(node)
                repo.record_version(
                    node, {"image" if kind == NodeKind.SHOT_FRAME else "video": path}
                )
        for stage in ("structure", "script", "frames", "final"):
            approve(session, project, stage, "Test owner")
        session.commit()
        return project.id, {(node.kind, node.shot_index): node.id for node in project.nodes}


def failed_frame(runtime: Runtime, project_id: str, node_id: str, *, paid: bool = False) -> str:
    with runtime.session_factory() as session:
        ProjectRepo(session).update_recipe(
            session.get(Node, node_id), {"visual_prompt": "new frame"}
        )
        session.commit()
    job_id = runtime.engine.rebuild(project_id, kinds={NodeKind.SHOT_FRAME}, node_ids={node_id})[0]
    claimed = runtime.queue.claim_next("api")
    assert claimed.id == job_id
    if paid:
        with runtime.session_factory() as session:
            job = session.get(Job, job_id)
            job.payload = {**job.payload, "_started_paid_attempts": [claimed.attempts]}
            session.commit()
    runtime.queue.fail(job_id, "temporary failure", retry=False)
    return job_id


def test_dashboard_reports_real_thumbnails_and_readiness_without_probe(workspace) -> None:
    client, runtime, application = workspace
    project_id, nodes = scene(runtime)
    application.state.connection_checks = {
        "comfyui:local": {"ok": True, "profile": "local"},
        "comfyui:remote": {"ok": False, "profile": "remote"},
    }
    result = client.get("/dashboard")
    assert result.status_code == 200
    card = result.json()["projects"][0]
    assert card["id"] == project_id and card["status"] == "clean"
    assert (
        card["thumbnail_kind"] == "image"
        and nodes[NodeKind.SHOT_FRAME, 1] in card["thumbnail_path"]
    )
    assert client.get("/media/" + card["thumbnail_path"]).status_code == 200
    assert result.json()["system"]["providers"]["openai"] is True
    assert result.json()["system"]["comfyui"]["status"] == "ok"
    assert "secret-provider-key" not in result.text


def test_dashboard_falls_back_to_final_and_ignores_missing_media(workspace) -> None:
    client, runtime, _ = workspace
    _, nodes = scene(runtime)
    with runtime.session_factory() as session:
        for node_id in (nodes[NodeKind.SHOT_FRAME, 1], nodes[NodeKind.SHOT_FRAME, 2]):
            node = session.get(Node, node_id)
            for path in node.current_version.output_paths.values():
                (runtime.settings.data_dir / path).unlink()
    card = client.get("/dashboard").json()["projects"][0]
    assert card["thumbnail_kind"] == "video"
    assert nodes[NodeKind.FINAL, None] in card["thumbnail_path"]
    (runtime.settings.data_dir / card["thumbnail_path"]).unlink()
    assert client.get("/dashboard").json()["projects"][0]["thumbnail_path"] is None


def test_jobs_global_filters_details_and_duration(workspace) -> None:
    client, runtime, _ = workspace
    project_id, _ = scene(runtime)
    job_id = runtime.queue.submit(
        "gpu",
        "tts",
        {"private_prompt": "do not expose"},
        project_id=project_id,
        model_key="voice-v1",
    )
    runtime.queue.submit("api", "image_gen", {}, project_id=project_id)
    runtime.queue.submit("gpu", "tts", {})
    with runtime.session_factory() as session:
        job = session.get(Job, job_id)
        job.status = JobStatus.FAILED
        job.started_at = datetime.now(timezone.utc) - timedelta(seconds=8)
        job.finished_at = job.started_at + timedelta(seconds=2.25)
        job.error = "Bearer hidden-token secret-provider-key"
        session.commit()
    response = client.get(
        "/jobs",
        params={"project_id": project_id, "lane": "gpu", "job_type": "tts", "status": "failed"},
    )
    assert response.status_code == 200
    assert len(response.json()) == 1
    item = response.json()[0]
    assert item["model_key"] == "voice-v1" and item["duration_seconds"] == 2.25
    assert item["project_title"] == "工作台演示"
    assert "private_prompt" not in response.text and "hidden-token" not in response.text
    assert "secret-provider-key" not in response.text
    assert len(client.get("/jobs?limit=1&offset=1").json()) == 1


@pytest.mark.parametrize("query", ["lane=bad", "status=bad", "limit=0", "offset=-1", "limit=1001"])
def test_jobs_reject_invalid_filters(workspace, query: str) -> None:
    client, _, _ = workspace
    assert client.get("/jobs?" + query).status_code == 422


def test_cancel_queued_job_releases_node_without_rescheduling(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    node_id = nodes[NodeKind.SHOT_FRAME, 1]
    with runtime.session_factory() as session:
        ProjectRepo(session).update_recipe(session.get(Node, node_id), {"prompt": "change"})
        session.commit()
    job_id = runtime.engine.rebuild(project_id, kinds={NodeKind.SHOT_FRAME})[0]
    response = client.post(f"/jobs/{job_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    with runtime.session_factory() as session:
        assert session.get(Node, node_id).status == NodeStatus.DIRTY
        assert reserved_cost(session, project_id) == 0
    assert not runtime.queue.list(project_id, JobStatus.QUEUED)
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409
    assert client.post("/jobs/missing/cancel").status_code == 404


def test_cancel_rejects_running_job_without_releasing_lane(workspace) -> None:
    client, runtime, _ = workspace
    job_id = runtime.queue.submit("gpu", "tts", {})
    runtime.queue.claim_next("gpu")
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 409
    assert runtime.queue.get(job_id).status == JobStatus.RUNNING


def test_retry_preserves_attempt_numbers_and_old_paid_reservations(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    job_id = failed_frame(runtime, project_id, nodes[NodeKind.SHOT_FRAME, 1], paid=True)
    response = client.post(f"/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text
    assert response.json()["attempts"] == 1 and response.json()["max_attempts"] == 4
    assert runtime.queue.get(job_id).payload["_started_paid_attempts"] == [1]
    with runtime.session_factory() as session:
        assert reserved_cost(session, project_id) == pytest.approx(0.4)
    claimed = runtime.queue.claim_next("api")
    assert claimed.attempts == 2
    runtime.check_job(claimed)
    with runtime.session_factory() as session:
        assert session.get(Project, project_id).spent == pytest.approx(0.2)
        assert reserved_cost(session, project_id) == pytest.approx(0.2)
        assert len(list(session.scalars(select(CostEntry)))) == 1


@pytest.mark.parametrize(
    "restriction,expected", [("consent", 403), ("review", 409), ("budget", 409), ("stale", 409)]
)
def test_retry_enforces_current_generation_gates(
    workspace, restriction: str, expected: int
) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    job_id = failed_frame(runtime, project_id, nodes[NodeKind.SHOT_FRAME, 1])
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        if restriction == "consent":
            project.character.consent_confirmed = False
        elif restriction == "review":
            session.delete(session.get(Review, (project_id, "script")))
        elif restriction == "budget":
            project.budget_limit = 0
        else:
            ProjectRepo(session).update_recipe(
                session.get(Node, nodes[NodeKind.SHOT_FRAME, 1]), {"visual_prompt": "changed again"}
            )
        session.commit()
    response = client.post(f"/jobs/{job_id}/retry")
    assert response.status_code == expected, response.text
    assert runtime.queue.get(job_id).status == JobStatus.FAILED


def test_retry_includes_outstanding_paid_attempt_in_budget(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    job_id = failed_frame(runtime, project_id, nodes[NodeKind.SHOT_FRAME, 1], paid=True)
    with runtime.session_factory() as session:
        session.get(Project, project_id).budget_limit = 0.3
        session.commit()
    assert client.post(f"/jobs/{job_id}/retry").status_code == 409
    assert runtime.queue.get(job_id).attempts == 1


def test_retry_rejects_nonfailed_unknown_and_duplicate_work(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    job_id = failed_frame(runtime, project_id, nodes[NodeKind.SHOT_FRAME, 1])
    runtime.queue.submit(
        "api", "image_gen", {}, project_id=project_id, node_id=nodes[NodeKind.SHOT_FRAME, 1]
    )
    assert client.post(f"/jobs/{job_id}/retry").status_code == 409
    assert client.post("/jobs/missing/retry").status_code == 404
    queued = runtime.queue.submit("cpu", "fake", {})
    assert client.post(f"/jobs/{queued}/retry").status_code == 409


def test_authoring_retry_rechecks_source_and_human_structure_review(workspace) -> None:
    client, runtime, _ = workspace
    project_id, _ = scene(runtime)
    job_id = runtime.queue.submit(
        "api", "analyze_video", {"estimated_cost": 0}, project_id=project_id
    )
    runtime.queue.claim_next("api")
    runtime.queue.fail(job_id, "failed", retry=False)
    assert client.post(f"/jobs/{job_id}/retry").status_code == 202
    assert client.post(f"/jobs/{job_id}/cancel").status_code == 200
    script = runtime.queue.submit(
        "api", "write_script", {"brief": "story", "estimated_cost": 0}, project_id=project_id
    )
    runtime.queue.claim_next("api")
    runtime.queue.fail(script, "failed", retry=False)
    with runtime.session_factory() as session:
        session.delete(session.get(Review, (project_id, "structure")))
        session.commit()
    assert client.post(f"/jobs/{script}/retry").status_code == 409


def test_costs_aggregate_all_filtered_rows_before_pagination(workspace) -> None:
    client, runtime, _ = workspace
    project_id, _ = scene(runtime)
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        ledger = CostLedger(session)
        for role, model, cost in (
            ("script", "small", 0.3),
            ("script", "small", 0.4),
            ("qa", "large", 0.8),
        ):
            job_id = runtime.queue.submit("api", role, {}, project_id=project_id, session=session)
            ledger.record_attempt(
                project,
                job_id=job_id,
                attempt=1,
                role=role,
                provider="fake",
                model=model,
                cost=cost,
            )
        session.commit()
    response = client.get(
        "/costs",
        params={
            "project_id": project_id,
            "provider": "fake",
            "role": "script",
            "model": "small",
            "limit": 1,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["entries"]) == 1 and data["totals"]["count"] == 2
    assert data["totals"]["est_cost"] == pytest.approx(0.7)
    assert data["by_role"] == [{"key": "script", "count": 2, "est_cost": pytest.approx(0.7)}]
    assert data["projects"][0]["spent"] == pytest.approx(1.5)
    assert client.get("/costs?role=missing").json()["totals"]["count"] == 0
    assert client.get("/costs?limit=0").status_code == 422


def test_budget_cannot_undercut_spend_reservations_or_unaccounted_attempts(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    failed_frame(runtime, project_id, nodes[NodeKind.SHOT_FRAME, 1], paid=True)
    with runtime.session_factory() as session:
        session.get(Project, project_id).spent = 1.0
        session.commit()
    runtime.queue.submit("gpu", "tts", {"estimated_cost": 0.5}, project_id=project_id)
    route = f"/projects/{project_id}/budget"
    assert client.put(route, json={"budget_limit": 1.6}).status_code == 409
    response = client.put(route, json={"budget_limit": 1.7})
    assert response.status_code == 200 and response.json()["remaining"] == pytest.approx(0)
    assert client.put(route, json={"budget_limit": None}).json()["budget_limit"] is None
    assert client.put(route, json={"budget_limit": -1}).status_code == 422
    assert client.put(route, json={"budget_limit": "NaN"}).status_code == 422
    assert client.put(route, json={"budget_limit": 3, "spent": 0}).status_code == 422
    assert client.put("/projects/missing/budget", json={"budget_limit": 1}).status_code == 404


def test_system_reports_local_readiness_versions_workers_and_metrics(
    workspace, monkeypatch
) -> None:
    client, runtime, application = workspace
    project_id, _ = scene(runtime)
    runtime.queue.submit("gpu", "tts", {}, project_id=project_id)
    monkeypatch.setattr("avd.api.workspace.shutil.which", lambda name: f"C:/tools/{name}.exe")

    class Supervisor:
        def status(self):
            return {"cpu": {"status": "running", "pid": 123}}

    application.state.worker_supervisor = Supervisor()
    response = client.get("/system")
    assert response.status_code == 200
    data = response.json()
    assert data["health"] == {"status": "ok", "database": "ok"}
    assert data["ffmpeg"]["ready"] is True and data["versions"]["python"]
    assert data["metrics"]["queue_depth"]["gpu"] == 1
    assert data["workers"]["cpu"]["pid"] == 123
    assert data["comfyui"]["status"] == "unverified"


def test_recent_logs_are_bounded_redacted_and_survive_key_replacement(workspace) -> None:
    client, runtime, _ = workspace
    logger = logging.getLogger("avd.test.workspace")
    for index in range(205):
        logger.warning("Diagnostic %s", index)
    logger.warning(
        'Credential secret-provider-key Bearer bearer-secret api_key=key-secret {"token":"json-secret"}'
    )
    runtime.settings.openai_api_key = SecretStr("replacement-key")
    response = client.get("/system/logs?limit=200")
    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len(lines) == 200
    assert not any("Diagnostic 0\n" in line for line in lines)
    assert all(
        value not in response.text
        for value in ("secret-provider-key", "bearer-secret", "key-secret", "json-secret")
    )
    assert "[REDACTED]" in response.text
    assert client.get("/system/logs?limit=201").status_code == 422


def test_redact_public_retains_types_but_removes_nested_credentials() -> None:
    settings = Settings(_env_file=None, openai_api_key="configured-value")
    data: Any = {
        "error": "configured-value https://alice:password@example.test",
        "nested": [{"token": "unknown-value", "number": 4}],
        "ready": True,
    }
    result = redact_public(data, settings)
    assert "configured-value" not in str(result) and "password" not in str(result)
    assert result["nested"][0] == {"token": "[REDACTED]", "number": 4}
    assert result["ready"] is True


def test_regenerate_frame_preserves_script_approval_and_targets_one_shot(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    with runtime.session_factory() as session:
        unrelated = session.get(Node, nodes[NodeKind.VOICEOVER, 2])
        ProjectRepo(session).update_recipe(unrelated, {"emotion": "calm"})
        session.commit()
    response = client.post(
        f"/projects/{project_id}/shots/1/regenerate", json={"kind": "shot_frame"}
    )
    assert response.status_code == 202, response.text
    result = response.json()
    assert len(result["job_ids"]) == 1 and result["requires_review"] is None
    job = runtime.queue.get(result["job_ids"][0])
    assert job.node_id == nodes[NodeKind.SHOT_FRAME, 1]
    assert nodes[NodeKind.VOICEOVER, 2] not in job.payload["selected_node_ids"]
    assert nodes[NodeKind.SHOT_FRAME, 2] not in job.payload["selected_node_ids"]
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        assert review_status(session, project)["script"]["approved"]
        assert not review_status(session, project)["frames"]["approved"]
        assert session.get(Node, nodes[NodeKind.SHOT_FRAME, 2]).status == NodeStatus.CLEAN


def test_regenerate_voice_completion_does_not_schedule_other_dirty_shots(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    with runtime.session_factory() as session:
        ProjectRepo(session).update_recipe(
            session.get(Node, nodes[NodeKind.VOICEOVER, 2]), {"emotion": "calm"}
        )
        session.commit()
    response = client.post(f"/projects/{project_id}/shots/1/regenerate", json={"kind": "voiceover"})
    assert response.status_code == 202, response.text
    job = runtime.queue.claim_next("gpu")
    assert job.node_id == nodes[NodeKind.VOICEOVER, 1]
    path = runtime.settings.data_dir / job.payload["output_dir"] / "audio.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"offline audio")
    completed = runtime.queue.complete(
        job.id,
        {"outputs": {"audio": path.relative_to(runtime.settings.data_dir).as_posix()}, "cost": 0},
    )
    runtime.engine.on_finished(completed)
    pending = runtime.queue.list(project_id, JobStatus.QUEUED)
    assert [item.node_id for item in pending] == [nodes[NodeKind.LIPSYNC, 1]]


def test_regeneration_keeps_dirty_change_when_new_budget_confirmation_required(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    runtime.settings.job_estimated_costs["image_gen"] = 2
    with runtime.session_factory() as session:
        approve_budget(session, session.get(Project, project_id), 10, "Owner")
        session.commit()
    response = client.post(f"/projects/{project_id}/shots/1/regenerate", json={})
    assert response.status_code == 202
    assert response.json()["job_ids"] == [] and response.json()["requires_review"]
    with runtime.session_factory() as session:
        node = session.get(Node, nodes[NodeKind.SHOT_FRAME, 1])
        assert node.status == NodeStatus.DIRTY and node.recipe["_regeneration"]
    assert not runtime.queue.list(project_id)


def test_regenerate_rejects_missing_shot_bad_kind_and_active_work(workspace) -> None:
    client, runtime, _ = workspace
    project_id, nodes = scene(runtime)
    assert client.post(f"/projects/{project_id}/shots/99/regenerate", json={}).status_code == 404
    assert (
        client.post(
            f"/projects/{project_id}/shots/1/regenerate", json={"kind": "analysis"}
        ).status_code
        == 422
    )
    runtime.queue.submit(
        "cpu", "final", {}, project_id=project_id, node_id=nodes[NodeKind.FINAL, None]
    )
    assert client.post(f"/projects/{project_id}/shots/1/regenerate", json={}).status_code == 409


def test_system_database_error_is_sanitized(workspace, monkeypatch) -> None:
    client, runtime, _ = workspace

    def fail():
        raise RuntimeError("database-secret")

    monkeypatch.setattr(runtime, "session_factory", fail)
    response = client.get("/system")
    assert response.status_code == 503 and "database-secret" not in response.text
