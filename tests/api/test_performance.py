"""Performance HTTP contracts use local persistent state and no model/network calls."""

from __future__ import annotations

import json
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient

from avd.api import performance as performance_api
from avd.api.app import create_app
from avd.config import Settings
from avd.jobs.models import JobStatus
from avd.performance.motion import motion_workflow_readiness
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, Project, Voice


@pytest.fixture
def api(tmp_path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source for offline API contract test")
    (tmp_path / "target.png").write_bytes(b"target for offline API contract test")
    with runtime.session_factory() as session:
        character = Character(
            name="actor", consent_confirmed=True, reference_images={"front": "target.png"}
        )
        voice = Voice(name="singer", consent_confirmed=True)
        session.add_all([character, voice])
        session.commit()
        config = {
            "preparation": {"source_video_path": "source.mp4"},
            "character_id": character.id,
            "voice_id": voice.id,
            "scene_prompt": "A moonlit forest stage",
        }
    try:
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            yield client, runtime, config
    finally:
        runtime.close()


def create(client):
    response = client.post(
        "/projects", json={"title": "Singing and dancing", "mode": "performance_remix"}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_configure_and_prepare_queue_only_preparation(api):
    client, runtime, config = api
    project_id = create(client)
    response = client.put(f"/projects/{project_id}/performance", json=config)
    assert response.status_code == 200, response.text
    assert len(response.json()["nodes"]) == 8
    queued = client.post(f"/projects/{project_id}/performance/prepare")
    assert queued.status_code == 202, queued.text
    assert not queued.json()["model_generated"]
    jobs = runtime.queue.list(project_id)
    assert len(jobs) == 1 and jobs[0].job_type == "performance_prepare"
    assert runtime.build_registry().get("performance_prepare") is not None
    assert client.post(f"/projects/{project_id}/performance/prepare").json()["job_ids"] == []
    assert client.put(f"/projects/{project_id}/performance", json=config).status_code == 409
    assert runtime.queue.cancel(jobs[0].id)
    assert runtime.queue.get(jobs[0].id).status == JobStatus.CANCELLED


def test_api_partial_update_and_voice_reference_listing_use_shared_contracts(api):
    client, runtime, config = api
    project_id = create(client)
    initial = client.put(f"/projects/{project_id}/performance", json=config).json()["config"]
    changed = client.patch(
        f"/projects/{project_id}/performance", json={"scene_prompt": "Snowy stage"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["config"]["preparation"] == initial["preparation"]
    assert changed.json()["config"]["voice_id"] == initial["voice_id"]
    assert changed.json()["config"]["scene_prompt"] == "Snowy stage"
    samples = client.get(f"/voices/{config['voice_id']}/singing-reference-samples")
    assert samples.status_code == 200, samples.text
    assert samples.json() == {"voice_id": config["voice_id"], "reference_samples": []}


def test_scene_patch_retains_frozen_separation_until_source_is_explicitly_resaved(api):
    from copy import deepcopy

    from avd.performance.separation import DemucsConfig
    from avd.state.models import NodeStatus
    from avd.state.repo import ProjectRepo

    client, runtime, config = api
    project_id = create(client)
    client.put(f"/projects/{project_id}/performance", json=config)
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        frozen = deepcopy(
            next(n.recipe for n in project.nodes if n.kind == NodeKind.VOCAL_SEPARATION)
        )
        for node in project.nodes:
            ProjectRepo(session).record_version(node, {})
        session.commit()
    runtime.settings.demucs_config = DemucsConfig(model_repo_relative="models/new-installation")
    response = client.patch(
        f"/projects/{project_id}/performance", json={"scene_prompt": "New stage"}
    )
    assert response.status_code == 200, response.text
    with runtime.session_factory() as session:
        nodes = {n.kind: n for n in session.get(Project, project_id).nodes}
        assert nodes[NodeKind.VOCAL_SEPARATION].recipe == frozen
        assert nodes[NodeKind.SINGING_VOICE].status == NodeStatus.CLEAN
        assert nodes[NodeKind.PERFORMANCE_MIX].status == NodeStatus.CLEAN
    response = client.patch(
        f"/projects/{project_id}/performance", json={"preparation": config["preparation"]}
    )
    assert response.status_code == 200, response.text
    with runtime.session_factory() as session:
        nodes = {n.kind: n for n in session.get(Project, project_id).nodes}
        assert (
            nodes[NodeKind.VOCAL_SEPARATION].recipe["demucs"]["config"]["model_repo_relative"]
            == "models/new-installation"
        )
        assert nodes[NodeKind.SINGING_VOICE].status == NodeStatus.DIRTY


def test_performance_source_upload_does_not_start_old_analysis(api):
    client, runtime, config = api
    project_id = create(client)
    response = client.post(
        f"/projects/{project_id}/source", files={"file": ("new.mp4", BytesIO(b"new video"))}
    )
    assert response.status_code == 201, response.text
    assert response.json()["analysis_job_id"] is None
    assert runtime.queue.list(project_id) == []


def test_input_review_can_be_approved_without_script_nodes(api):
    client, runtime, config = api
    project_id = create(client)
    client.put(f"/projects/{project_id}/performance", json=config)
    response = client.post(
        f"/projects/{project_id}/approve",
        json={"stage": "performance_inputs", "approved_by": "owner"},
    )
    assert response.status_code == 200, response.text
    status = client.get(f"/projects/{project_id}/status")
    assert status.json()["reviews"]["performance_inputs"]["approved"]
    assert "script" not in status.json()["reviews"]


def test_missing_configuration_and_wrong_project_mode_are_explicit(api):
    client, runtime, config = api
    project_id = create(client)
    assert client.post(f"/projects/{project_id}/performance/prepare").status_code == 409
    old = client.post("/projects", json={"title": "old"}).json()["id"]
    assert client.put(f"/projects/{old}/performance", json=config).status_code == 409
    assert client.get("/projects/missing/performance").status_code == 404


def test_configuration_rejects_unauthorized_assets_and_absolute_paths(api):
    client, runtime, config = api
    project_id = create(client)
    bad = {**config, "preparation": {"source_video_path": "D:/outside.mp4"}}
    assert client.put(f"/projects/{project_id}/performance", json=bad).status_code == 422
    with runtime.session_factory() as session:
        session.get(Voice, config["voice_id"]).consent_confirmed = False
        session.commit()
    assert client.put(f"/projects/{project_id}/performance", json=config).status_code == 403
    with runtime.session_factory() as session:
        assert session.get(Project, project_id).nodes == []


def test_replacing_source_invalidates_preparation_without_adding_analysis(api):
    client, runtime, config = api
    project_id = create(client)
    client.put(f"/projects/{project_id}/performance", json=config)
    response = client.post(
        f"/projects/{project_id}/source", files={"file": ("next.mp4", b"different source")}
    )
    assert response.status_code == 201, response.text
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        assert (
            project.performance_config["preparation"]["source_video_path"]
            == project.source_video_path
        )
        assert all(n.kind != NodeKind.ANALYSIS for n in project.nodes)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("analyze_video", {}),
        ("write_script", {"brief": "sing"}),
        ("generate_voiceover", {}),
    ],
)
def test_old_authoring_tools_cannot_inject_script_or_tts_into_performance(api, tool, args):
    client, runtime, config = api
    project_id = create(client)
    response = client.post(f"/projects/{project_id}/tools/{tool}", json={"args": args})
    assert response.status_code == 422, response.text
    assert runtime.queue.list(project_id) == []


def test_model_import_is_versioned_and_does_not_claim_inference_readiness(api):
    client, runtime, config = api
    (runtime.settings.data_dir / "offline.pth").write_bytes(
        b"FAKE CHECKPOINT FOR IMPORT CONTRACT ONLY"
    )
    body = {
        "voice_id": config["voice_id"],
        "model_path": "offline.pth",
        "sample_rate": 40000,
        "provenance": "Offline API contract fixture, never used for inference",
    }
    response = client.post("/singing-models/import", json=body)
    assert response.status_code == 201, response.text
    model = response.json()
    assert model["number"] == 1
    assert model["model_path"].startswith("assets/")
    again = client.post("/singing-models/import", json=body)
    assert again.json()["id"] == model["id"]
    listed = client.get("/singing-models", params={"voice_id": config["voice_id"]}).json()["models"]
    assert [row["id"] for row in listed] == [model["id"]]
    state = client.get(f"/singing-models/{model['id']}/readiness").json()
    assert state["imported_files_valid"]
    assert not state["ready_for_inference"] and not state["quality_verified"]
    assert client.get("/singing-models", params={"voice_id": "other"}).json()["models"] == []


def test_invalid_singing_model_is_rejected_before_writing_configuration(api):
    client, runtime, config = api
    project_id = create(client)
    response = client.put(
        f"/projects/{project_id}/performance",
        json={
            **config,
            "voice_model_version_id": "not-a-model",
        },
    )
    assert response.status_code == 422
    with runtime.session_factory() as session:
        assert session.get(Project, project_id).performance_config is None


def test_preview_and_generation_explain_missing_models_without_enqueuing_fake_media(api):
    client, runtime, config = api
    project_id = create(client)
    assert client.put(f"/projects/{project_id}/performance", json=config).status_code == 200
    state = client.get(f"/projects/{project_id}/performance/readiness").json()
    assert state["configured"] and state["source_preparation"]["ready"]
    assert not state["voice"]["ready_for_inference"]
    assert not state["ready_for_preview"] and not state["quality_verified"]
    client.post(
        f"/projects/{project_id}/approve",
        json={
            "stage": "performance_inputs",
            "approved_by": "owner",
        },
    )
    response = client.post(f"/projects/{project_id}/performance/preview")
    assert response.status_code == 409
    assert not response.json()["detail"]["readiness"]["ready_for_preview"]
    assert runtime.queue.list(project_id) == []


@pytest.mark.parametrize("native_sampling", [False, True])
def test_motion_workflow_configuration_and_readiness_use_frozen_export(
    api, monkeypatch, native_sampling
):
    client, runtime, config = api
    template = {
        "9": {
            "class_type": "OfflineMotionFixture",
            "inputs": {
                "video": "{{motion_video}}",
                "image": "{{reference_image}}",
                "seed": "{{seed}}",
            },
        }
    }
    path = runtime.settings.data_dir / "offline-workflow.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    configured = {
        **config,
        "motion_workflow": {
            "template_path": str(path),
            "revision": "offline-v1",
            "model_versions": {"fixture": "v1"},
            "output_node_id": "9",
            "expected_frame_count": 160,
        },
        "motion_parameters": {"seed": 7},
    }
    if native_sampling:
        configured["motion_workflow"]["input_sampling"] = {
            "frame_rate": "16/1",
            "frame_count": 160,
            "tail_padding_frames": 1,
            "output_tail_padding_frames": 1,
        }
        configured["motion_workflow"]["max_input_frames"] = 161
    monkeypatch.setattr(
        performance_api,
        "motion_workflow_readiness",
        lambda *args, **kwargs: motion_workflow_readiness(
            *args,
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"OfflineMotionFixture": {"input": {}}})
            ),
        ),
    )
    project_id = create(client)
    response = client.put(f"/projects/{project_id}/performance", json=configured)
    assert response.status_code == 200, response.text
    assert response.json()["config"]["motion_parameters"] == {"seed": 7}
    if native_sampling:
        assert response.json()["config"]["motion_workflow"]["input_sampling"]["frame_count"] == 160
    path.write_text("{}", encoding="utf-8")
    state = client.get(f"/projects/{project_id}/performance/readiness").json()
    assert state["motion"]["configured"] and state["motion"]["ready"]
    assert state["motion"]["expected_frame_count"] == 160
    if native_sampling:
        assert state["motion"]["input_sampling"]["tail_padding_frames"] == 1
        assert state["motion"]["uploaded_frame_count"] == 161
        assert state["motion"]["sampled_performance_duration_s"] == 10
        invalid = {**configured, "preparation": {**configured["preparation"], "duration_s": 8}}
        rejected = client.put(f"/projects/{project_id}/performance", json=invalid)
        assert rejected.status_code == 422
        assert "sampling must cover" in rejected.text
    assert not state["motion"]["quality_verified"]
    assert not state["ready_for_generation"]  # No usable voice model was supplied.
