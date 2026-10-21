"""Execution regressions found while reviewing the production remix backend."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.assets.media import MediaAsset
from avd.backends.comfyui import ComfyUIClient
from avd.config import Settings
from avd.executors import build_registry
from avd.jobs.models import Job
from avd.performance import backend as performance_backend
from avd.performance import motion
from avd.performance.backend import LocalPerformanceBackend
from avd.performance.graph import configure_performance
from avd.performance.models import PerformanceConfig, PreparationRequest
from avd.performance.motion import MotionWorkflowSpec, snapshot_motion_workflow
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, Project, Voice


@pytest.fixture
def configured(tmp_path: Path) -> Any:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    (tmp_path / "source.mp4").write_bytes(b"EXPLICIT OFFLINE SOURCE FIXTURE")
    (tmp_path / "character.png").write_bytes(b"EXPLICIT OFFLINE CHARACTER FIXTURE")
    folder = tmp_path / "assets/media/source"
    folder.mkdir(parents=True)
    (folder / "vocals.wav").write_bytes(b"OFFLINE SNAPSHOT ONLY; NEVER SENT TO AUDIO TOOLS")
    (folder / "no_vocals.wav").write_bytes(b"OFFLINE SNAPSHOT ONLY; ACCOMPANIMENT")
    with runtime.session_factory() as session:
        character = Character(
            name="target", reference_images={"front": "character.png"}, consent_confirmed=True
        )
        voice = Voice(name="singer", consent_confirmed=True)
        session.add_all([character, voice])
        session.flush()
        project = Project(title="backend review", mode="performance_remix", budget_limit=None)
        session.add(project)
        session.add_all(
            MediaAsset(
                name=name, kind="audio", path=f"assets/media/source/{name}", source="source.mp4"
            )
            for name in ("vocals.wav", "no_vocals.wav")
        )
        session.flush()
        config = PerformanceConfig(
            preparation=PreparationRequest(source_video_path="source.mp4"),
            character_id=character.id,
            voice_id=voice.id,
            scene_prompt="Moonlit stage",
        )
        nodes = configure_performance(session, project, config, tmp_path)
        scene = deepcopy(nodes[NodeKind.SCENE_REFERENCE.value].recipe)
        project_id = project.id
        session.commit()
    try:
        yield runtime, project_id, scene
    finally:
        runtime.close()


def test_scene_reference_verifies_current_image_hash_before_model_generation(configured) -> None:
    runtime, _, scene = configured
    root = runtime.settings.data_dir
    (root / "character.png").write_bytes(b"DIFFERENT TARGET IMAGE AFTER CONFIGURATION")
    calls: list[str] = []

    class FakeImage:
        def generate(self, *, prompt, reference_images, output_path, model):
            calls.append("paid_generation_started")
            output_path.write_bytes(b"OFFLINE IMAGE OUTPUT")
            return output_path

    registry = build_registry(runtime.settings, image_backend=FakeImage())
    job = Job(
        lane="api",
        job_type="scene_reference",
        attempts=1,
        payload={
            "data_dir": str(root),
            "output_dir": "projects/backend-review/scene",
            "recipe": scene,
            "inputs": {},
            "context": {},
        },
    )
    with pytest.raises((ValueError, RuntimeError), match="hash|changed|reference"):
        registry.get("scene_reference")(job)
    assert calls == []


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_readiness_refuses_a_missing_or_replaced_stem_snapshot(configured, change: str) -> None:
    runtime, project_id, _ = configured
    with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
        before = client.get(f"/projects/{project_id}/performance/readiness")
        assert before.status_code == 200
        assert before.json()["separation"]["ready"]
        vocals = runtime.settings.data_dir / "assets/media/source/vocals.wav"
        if change == "delete":
            vocals.unlink()
        else:
            vocals.write_bytes(b"STEM REPLACED AFTER CONFIGURATION")
        after = client.get(f"/projects/{project_id}/performance/readiness")
        assert after.status_code == 200
        assert not after.json()["separation"]["ready"]
        assert after.json()["separation"]["reasons"]
        assert not after.json()["ready_for_preview"]


def test_production_motion_handler_consumes_prepared_video_scene_and_frozen_route(
    configured, monkeypatch
):
    runtime, _, _ = configured
    root = runtime.settings.data_dir
    path = root / "offline-export.json"
    path.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflineMotionFixture",
                    "inputs": {
                        "source": "{{motion_video}}",
                        "reference": "{{reference_image}}",
                        "seed": "{{seed}}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = snapshot_motion_workflow(
        MotionWorkflowSpec(
            template_path=path,
            revision="offline-only",
            model_versions={"fixture": "v1"},
            output_node_id="9",
        ),
        "motion_video",
    )
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert request.url.port == 8189
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded-fixture.bin"})
        if request.url.path == "/prompt":
            assert json.loads(request.content)["prompt"]["9"]["inputs"]["seed"] == 7
            return httpx.Response(200, json={"prompt_id": "production-contract-fixture"})
        if request.url.path.startswith("/history/"):
            return httpx.Response(
                200,
                json={
                    "production-contract-fixture": {
                        "status": {"completed": True},
                        "outputs": {
                            "9": {"videos": [{"filename": "result.mp4", "type": "output"}]}
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"EXPLICIT OFFLINE GENERATED OUTPUT FIXTURE")
        pytest.fail(str(request.url))

    monkeypatch.setattr(
        performance_backend,
        "ComfyUIClient",
        lambda base_url, timeout: ComfyUIClient(
            base_url, timeout=timeout, transport=httpx.MockTransport(handle)
        ),
    )
    monkeypatch.setattr(
        motion,
        "_video_timing",
        lambda path, tools: {
            "width": 32,
            "height": 32,
            "frame_count": 2,
            "frame_timestamps_s": [0, 0.5],
            "duration_s": 1,
            "time_base": "1/10000",
            "tick_s": 0.0001,
        },
    )
    paid = []
    registry = build_registry(
        runtime.settings,
        before_paid=lambda current: paid.append(current.attempts),
        performance_backend=LocalPerformanceBackend(runtime.settings, runtime.session_factory),
    )
    job = Job(
        lane="gpu",
        job_type="motion_video",
        attempts=1,
        payload={
            "data_dir": str(root),
            "output_dir": "projects/offline-motion/job",
            "estimated_cost": 0.5,
            "recipe": {
                "workflow": snapshot,
                "parameters": {"seed": 7},
                "comfyui_profile": {"base_url": "http://127.0.0.1:8189", "timeout": 2},
            },
            "inputs": {
                "prepared": {"kind": "performance_prepare", "outputs": {"motion": "source.mp4"}},
                "scene": {"kind": "scene_reference", "outputs": {"image": "character.png"}},
            },
            "context": {},
        },
    )
    result = registry.get("motion_video")(job)
    manifest = json.loads((root / result["outputs"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["prompt_id"] == "production-contract-fixture"
    assert not manifest["quality_verified"]
    assert result["outputs"]["video"].startswith("projects/offline-motion/job/attempt-1/")
    assert (root / "projects/offline-motion/job/remote-submission.json").is_file()
    assert result["cost"] == 0.5 and paid == [1]
    job.attempts = 2
    resumed = registry.get("motion_video")(job)
    assert resumed["cost"] == 0
    assert paid == [1]
    assert calls.count("/prompt") == 1
