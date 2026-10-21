"""Short video-preview HTTP contracts, using offline readiness and executor stubs.

The fake outputs below are text markers, never evidence of real model quality.
No model, subprocess, network service or live asset database is used.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.config import Settings
from avd.jobs import HandlerRegistry, Worker
from avd.jobs.models import Job, JobStatus
from avd.performance.service import PerformanceService
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, NodeStatus, Project, Voice

REFERENCE_PREVIEW_KINDS = {
    "performance_prepare",
    "vocal_separation",
    "scene_reference",
    "singing_voice",
    "performance_mix",
}
VIDEO_PREVIEW_KINDS = REFERENCE_PREVIEW_KINDS | {
    "motion_video",
    "performance_sync",
    "performance_final",
}


@pytest.fixture
def video_preview_api(tmp_path, monkeypatch) -> Iterator[dict[str, Any]]:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path, confirm_threshold=None))
    (tmp_path / "source.mp4").write_bytes(b"OFFLINE SOURCE MARKER, NOT MEDIA")
    (tmp_path / "target.png").write_bytes(b"OFFLINE CHARACTER MARKER, NOT MEDIA")
    with runtime.session_factory() as session:
        character = Character(
            name="offline target",
            consent_confirmed=True,
            reference_images={"front": "target.png"},
        )
        voice = Voice(name="offline voice", consent_confirmed=True)
        session.add_all([character, voice])
        session.commit()
        config = {
            "preparation": {"source_video_path": "source.mp4", "duration_s": 10},
            "character_id": character.id,
            "voice_id": voice.id,
            "scene_prompt": "Offline replacement stage",
        }
    state = {
        "configured": True,
        "source_preparation": {"ready": True, "reasons": []},
        "separation": {"ready": True, "reasons": []},
        "voice": {"ready_for_inference": True, "reasons": []},
        "motion": {"ready": True, "reasons": []},
        "lipsync": {"ready": True, "reasons": []},
        "ready_for_preview": True,
        "ready_for_generation": True,
        "quality_verified": False,
    }
    monkeypatch.setattr(PerformanceService, "readiness", lambda self, project_id: deepcopy(state))
    try:
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            yield {"client": client, "runtime": runtime, "config": config, "state": state}
    finally:
        runtime.close()


def configured_project(
    harness: dict[str, Any], *, duration_s: float = 10, lipsync: bool = False
) -> str:
    client = harness["client"]
    response = client.post(
        "/projects", json={"title": "Offline video preview", "mode": "performance_remix"}
    )
    assert response.status_code == 201, response.text
    project_id = response.json()["id"]
    config = deepcopy(harness["config"])
    config["preparation"]["duration_s"] = duration_s
    config["lipsync"] = lipsync
    response = client.put(f"/projects/{project_id}/performance", json=config)
    assert response.status_code == 200, response.text
    return project_id


def approve_inputs(harness: dict[str, Any], project_id: str) -> None:
    response = harness["client"].post(
        f"/projects/{project_id}/approve",
        json={"stage": "performance_inputs", "approved_by": "offline test owner"},
    )
    assert response.status_code == 200, response.text


def drain_offline_graph(harness: dict[str, Any], project_id: str) -> list[str]:
    runtime = harness["runtime"]
    calls: list[str] = []
    registry = HandlerRegistry()

    def fake(job: Job) -> dict[str, Any]:
        # Exercise the execution-time gate too, without starting a real worker process.
        runtime.check_job(job)
        calls.append(job.job_type)
        folder = runtime.settings.data_dir / job.payload["output_dir"] / f"attempt-{job.attempts}"
        folder.mkdir(parents=True)
        is_video = job.job_type in {
            "motion_video",
            "performance_sync",
            "performance_lipsync",
            "performance_final",
        }
        output = folder / ("offline-video.mp4" if is_video else "offline-result.txt")
        output.write_text("OFFLINE EXECUTOR MARKER, NOT GENERATED MEDIA", encoding="utf-8")
        return {
            "outputs": {
                "video" if is_video else "test": output.relative_to(
                    runtime.settings.data_dir
                ).as_posix()
            },
            "cost": 0,
        }

    for kind in VIDEO_PREVIEW_KINDS | {"performance_lipsync"}:
        registry.register(kind, fake)
    workers = [
        Worker(runtime.queue, lane, registry, on_finished=runtime.engine.on_finished)
        for lane in ("cpu", "gpu", "api")
    ]
    for _ in range(20):
        worked = [worker.run_once() for worker in workers]
        if not any(worked):
            break
    jobs = runtime.queue.list(project_id)
    assert jobs and all(job.status == JobStatus.SUCCEEDED for job in jobs), [
        (job.job_type, job.status.value, job.error) for job in jobs
    ]
    return calls


def test_video_preview_requires_current_input_approval_before_queueing(video_preview_api):
    harness = video_preview_api
    project_id = configured_project(harness)
    response = harness["client"].post(f"/projects/{project_id}/performance/video-preview")
    assert response.status_code == 409, response.text
    assert "performance_inputs" in response.text
    assert harness["runtime"].queue.list(project_id) == []


@pytest.mark.parametrize("unready_stage", ["motion", "lipsync"])
def test_video_preview_rejects_unready_video_workflows_without_jobs(
    video_preview_api, unready_stage
):
    harness = video_preview_api
    project_id = configured_project(harness, lipsync=unready_stage == "lipsync")
    approve_inputs(harness, project_id)
    harness["state"][unready_stage] = {"ready": False, "reasons": ["Offline missing workflow"]}
    harness["state"]["ready_for_generation"] = False
    # Reference/audio readiness alone must not allow a complete video preview.
    assert harness["state"]["ready_for_preview"]
    response = harness["client"].post(f"/projects/{project_id}/performance/video-preview")
    assert response.status_code == 409, response.text
    assert "Offline missing workflow" in response.text
    assert harness["runtime"].queue.list(project_id) == []


@pytest.mark.parametrize("duration_s", [1, 10])
@pytest.mark.parametrize("lipsync", [False, True])
def test_video_preview_schedules_the_complete_short_graph_after_input_review(
    video_preview_api, monkeypatch, duration_s, lipsync
):
    harness = video_preview_api
    runtime = harness["runtime"]
    project_id = configured_project(harness, duration_s=duration_s, lipsync=lipsync)
    approve_inputs(harness, project_id)
    rebuild = runtime.engine.rebuild
    requests: list[dict[str, Any]] = []

    def capture(project_id, **kwargs):
        requests.append(kwargs)
        return rebuild(project_id, **kwargs)

    monkeypatch.setattr(runtime.engine, "rebuild", capture)
    response = harness["client"].post(f"/projects/{project_id}/performance/video-preview")
    assert response.status_code == 202, response.text
    assert response.json()["stage"] == "performance_video_preview"
    assert response.json()["job_ids"]
    assert response.json()["model_generated"] is False
    assert requests[0].get("kinds") is None
    expected = VIDEO_PREVIEW_KINDS | ({"performance_lipsync"} if lipsync else set())
    assert set(drain_offline_graph(harness, project_id)) == expected
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        assert project.performance_config["preparation"]["duration_s"] == duration_s
        assert all(node.status == NodeStatus.CLEAN for node in project.nodes)
        assert any(node.kind == NodeKind.PERFORMANCE_FINAL for node in project.nodes)


@pytest.mark.parametrize("duration_s", [10.001, 20])
def test_video_preview_rejects_long_interval_without_silently_trimming(
    video_preview_api, duration_s
):
    harness = video_preview_api
    project_id = configured_project(harness, duration_s=duration_s)
    approve_inputs(harness, project_id)
    response = harness["client"].post(f"/projects/{project_id}/performance/video-preview")
    assert response.status_code == 409, response.text
    assert "10-second" in response.text
    assert harness["runtime"].queue.list(project_id) == []
    with harness["runtime"].session_factory() as session:
        assert (
            session.get(Project, project_id).performance_config["preparation"]["duration_s"]
            == duration_s
        )


def test_original_preview_remains_reference_and_singing_only(video_preview_api):
    harness = video_preview_api
    project_id = configured_project(harness)
    approve_inputs(harness, project_id)
    # Compatibility preview remains usable even when video generation is unavailable.
    harness["state"]["motion"] = {"ready": False, "reasons": ["Offline missing workflow"]}
    harness["state"]["ready_for_generation"] = False
    response = harness["client"].post(f"/projects/{project_id}/performance/preview")
    assert response.status_code == 202, response.text
    assert response.json()["stage"] == "reference_and_singing_preview"
    assert set(drain_offline_graph(harness, project_id)) == REFERENCE_PREVIEW_KINDS
    with harness["runtime"].session_factory() as session:
        project = session.get(Project, project_id)
        assert {
            node.kind.value for node in project.nodes if node.status == NodeStatus.CLEAN
        } == REFERENCE_PREVIEW_KINDS
