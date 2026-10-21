"""Regression cases found by an independent review of the new mode."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from avd.api.app import create_app
from avd.config import Settings
from avd.director.review import approve
from avd.executors import build_registry
from avd.jobs import Worker
from avd.jobs.models import Job, JobStatus
from avd.llm.ledger import CostEntry
from avd.performance.graph import configure_performance
from avd.performance.models import PerformanceConfig, PreparationRequest
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def performance_runtime(tmp_path: Path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    (tmp_path / "source.mp4").write_bytes(b"offline reference")
    (tmp_path / "character.png").write_bytes(b"offline character")
    with runtime.session_factory() as session:
        character = Character(
            name="target", reference_images={"front": "character.png"}, consent_confirmed=True
        )
        voice = Voice(name="singing target", consent_confirmed=True)
        session.add_all([character, voice])
        session.flush()
        project = Project(
            title="performance regression", mode="performance_remix", budget_limit=None
        )
        session.add(project)
        session.flush()
        config = PerformanceConfig(
            preparation=PreparationRequest(source_video_path="source.mp4"),
            character_id=character.id,
            voice_id=voice.id,
            scene_prompt="Warm recording studio",
        )
        configure_performance(session, project, config, tmp_path)
        project_id = project.id
        session.commit()
    try:
        yield runtime, project_id, config
    finally:
        runtime.close()


def test_scene_change_preserves_mix_settings_and_confirmed_mix(performance_runtime):
    runtime, project_id, config = performance_runtime
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        repo = ProjectRepo(session)
        mix = next(node for node in project.nodes if node.kind == NodeKind.PERFORMANCE_MIX)
        repo.update_recipe(mix, {"vocal_gain": 0.6, "accompaniment_gain": 0.8})
        version = repo.record_version(mix, {})
        configure_performance(
            session,
            project,
            config.model_copy(update={"scene_prompt": "Moonlit outdoor stage"}),
            runtime.settings.data_dir,
        )
        assert mix.recipe["vocal_gain"] == 0.6
        assert mix.recipe["accompaniment_gain"] == 0.8
        assert mix.status == NodeStatus.CLEAN
        assert mix.current_version_id == version.id


def test_failed_preparation_can_retry_from_browser_without_legacy_frames_review(
    performance_runtime,
):
    runtime, project_id, _ = performance_runtime
    [job_id] = runtime.engine.rebuild(project_id, kinds={NodeKind.PERFORMANCE_PREPARE})
    with runtime.session_factory() as session:
        job = session.get(Job, job_id)
        job.status = JobStatus.FAILED
        job.error = "temporary media tool failure"
        node = next(
            n
            for n in session.get(Project, project_id).nodes
            if n.kind == NodeKind.PERFORMANCE_PREPARE
        )
        node.status = NodeStatus.FAILED
        session.commit()
    with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
        response = client.post(f"/jobs/{job_id}/retry")
    assert response.status_code == 202, response.text
    assert runtime.queue.get(job_id).status == JobStatus.QUEUED


def test_scene_image_attempt_records_selected_image_provider_and_model(performance_runtime):
    runtime, project_id, _ = performance_runtime
    runtime.settings.image_provider = "grok"
    runtime.settings.grok_image_model = "grok-imagine-image-2.0"
    runtime.settings.job_estimated_costs["scene_reference"] = 0.05
    with runtime.session_factory() as session:
        approve(session, session.get(Project, project_id), "performance_inputs", "tester")
        session.commit()

    class FakeImage:
        def generate(self, *, prompt, reference_images, output_path, model):
            assert model == "grok-imagine-image-2.0"
            output_path.write_bytes(b"EXPLICIT OFFLINE FAKE IMAGE")
            return output_path

    [job_id] = runtime.engine.rebuild(project_id, kinds={NodeKind.SCENE_REFERENCE})
    registry = build_registry(runtime.settings, image_backend=FakeImage())
    assert Worker(runtime.queue, "api", registry, on_finished=runtime.engine.on_finished).run_once()
    with runtime.session_factory() as session:
        entry = session.scalar(select(CostEntry).where(CostEntry.job_id == job_id))
        assert entry.provider == "grok"
        assert entry.model == "grok-imagine-image-2.0"
        assert entry.est_cost == pytest.approx(0.05)


def test_browser_cancellation_survives_preparation_completion(performance_runtime):
    runtime, project_id, _ = performance_runtime
    with runtime.session_factory() as session:
        approve(session, session.get(Project, project_id), "performance_inputs", "tester")
        session.commit()
    job_ids = runtime.engine.rebuild(project_id)
    scene = next(
        runtime.queue.get(job_id)
        for job_id in job_ids
        if runtime.queue.get(job_id).job_type == "scene_reference"
    )
    with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
        response = client.post(f"/jobs/{scene.id}/cancel")
        assert response.status_code == 200
        preparation = runtime.queue.claim_next("cpu")
        assert preparation is not None and preparation.job_type == "performance_prepare"
        output = (
            runtime.settings.data_dir
            / preparation.payload["output_dir"]
            / "offline-preparation.json"
        )
        output.parent.mkdir(parents=True)
        output.write_text('{"offline_fake": true}', encoding="utf-8")
        completed = runtime.queue.complete(
            preparation.id,
            {"outputs": {"manifest": output.relative_to(runtime.settings.data_dir).as_posix()}},
        )
        runtime.engine.on_finished(completed)
        scenes = [
            job for job in runtime.queue.list(project_id) if job.job_type == "scene_reference"
        ]
        assert len(scenes) == 1
        assert scenes[0].status == JobStatus.CANCELLED
