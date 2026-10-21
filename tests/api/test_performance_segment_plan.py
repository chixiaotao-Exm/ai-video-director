"""Read-only interval-plan HTTP contracts with schema-valid offline provenance.

Media files are explicit text markers. No model, media probe or live server runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from avd.api.app import create_app
from avd.config import Settings
from avd.director.review import Review
from avd.jobs.models import Job
from avd.performance.models import PreparedPerformance
from avd.runtime import Runtime
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Version, Voice
from avd.state.repo import ProjectRepo


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def state_snapshot(runtime: Runtime) -> dict[str, Any]:
    """Capture the persisted state that a planning-only request must not change."""
    result: dict[str, Any] = {}
    with runtime.session_factory() as session:
        for model in (Project, Node, Version, Review, Job):
            mapper = inspect(model)
            result[model.__tablename__] = [
                {column.key: deepcopy(getattr(row, column.key)) for column in mapper.columns}
                for row in session.scalars(select(model).order_by(*mapper.primary_key))
            ]
        result["node_dependencies"] = {
            node.id: sorted(parent.id for parent in node.depends_on)
            for node in session.scalars(select(Node))
        }
    return result


def input_files_snapshot(harness: dict[str, Any]) -> dict[str, bytes]:
    return {
        name: harness[name].read_bytes()
        for name in ("source", "motion", "audio", "manifest", "template")
    }


@pytest.fixture
def segment_plan_api(tmp_path) -> Iterator[dict[str, Any]]:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path, confirm_threshold=None))
    source = tmp_path / "source.mp4"
    source.write_bytes(b"OFFLINE ORIGINAL SOURCE MARKER, NOT MEDIA")
    (tmp_path / "target.png").write_bytes(b"OFFLINE CHARACTER MARKER, NOT MEDIA")
    prepared_dir = tmp_path / "prepared"
    prepared_dir.mkdir()
    motion = prepared_dir / "motion.mp4"
    audio = prepared_dir / "audio.wav"
    motion.write_bytes(b"OFFLINE PREPARED MOTION MARKER, NOT MEDIA")
    audio.write_bytes(b"OFFLINE PREPARED AUDIO MARKER, NOT MEDIA")
    template = tmp_path / "workflow.json"
    template.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflinePlanningFixture",
                    "inputs": {
                        "motion": "{{motion_video}}",
                        "reference": "{{reference_image}}",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
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
            "preparation": {
                "source_video_path": "source.mp4",
                "start_s": 2.5,
                "duration_s": 10,
                "sample_rate": 48000,
            },
            "character_id": character.id,
            "voice_id": voice.id,
            "scene_prompt": "Offline stage",
            "motion_workflow": {
                "template_path": str(template),
                "revision": "offline-plan-api-v1",
                "model_versions": {"offline-model": "never-loaded"},
                "output_node_id": "9",
                "expected_frame_count": 160,
                "max_input_frames": 161,
                "max_duration_s": 10,
                "input_sampling": {
                    "frame_rate": "16/1",
                    "frame_count": 160,
                    "tail_padding_frames": 1,
                    "output_tail_padding_frames": 1,
                },
            },
        }
    prepared = PreparedPerformance.model_validate(
        {
            "source": {
                "path": "source.mp4",
                "sha256": digest(source.read_bytes()),
                "size_bytes": source.stat().st_size,
                "container_start_time_s": 12.5,
                "container_duration_s": 65,
                "video": {
                    "index": 0,
                    "time_base": "1/60000",
                    "start_time_s": 12.5,
                    "duration_s": 65,
                    "width": 720,
                    "height": 1280,
                    "avg_frame_rate": "60/1",
                },
                "audio": {
                    "index": 1,
                    "time_base": "1/48000",
                    "start_time_s": 12.5,
                    "duration_s": 65,
                    "sample_rate": 48000,
                    "channels": 2,
                },
                "audio_offset_s": 0,
            },
            "request": config["preparation"],
            "timeline": {
                "requested_start_s": 2.5,
                "requested_duration_s": 10,
                "source_origin_pts_time_s": 12.5,
                "source_interval_start_pts_time_s": 15,
                "source_interval_end_pts_time_s": 25,
                "actual_duration_s": 10,
                "frame_count": 600,
                "frame_time_tolerance_s": 1 / 60,
                "anchors": [
                    {
                        "frame_index": index,
                        "output_time_s": index / 60,
                        "source_pts_time_s": 15 + index / 60,
                        "source_video_time_s": 2.5 + index / 60,
                    }
                    for index in range(600)
                ],
            },
            "motion": {
                "path": "prepared/motion.mp4",
                "sha256": digest(motion.read_bytes()),
                "size_bytes": motion.stat().st_size,
                "width": 720,
                "height": 1280,
                "frame_count": 600,
                "time_base": "1/60000",
                "avg_frame_rate": "60/1",
            },
            "audio": {
                "path": "prepared/audio.wav",
                "sha256": digest(audio.read_bytes()),
                "size_bytes": audio.stat().st_size,
                "sample_rate": 48000,
                "channels": 2,
                "sample_count": 480000,
                "duration_s": 10,
                "leading_silence_s": 0,
            },
            "ffmpeg_version": "offline-schema-fixture",
            "ffprobe_version": "offline-schema-fixture",
        }
    )
    manifest = prepared_dir / "manifest.json"
    manifest.write_text(prepared.model_dump_json(), encoding="utf-8")
    try:
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            response = client.post(
                "/projects", json={"title": "Offline interval plan", "mode": "performance_remix"}
            )
            assert response.status_code == 201, response.text
            project_id = response.json()["id"]
            response = client.put(f"/projects/{project_id}/performance", json=config)
            assert response.status_code == 200, response.text
            with runtime.session_factory() as session:
                project = session.get(Project, project_id)
                prepare = next(
                    node for node in project.nodes if node.kind == NodeKind.PERFORMANCE_PREPARE
                )
                ProjectRepo(session).record_version(
                    prepare,
                    {
                        "motion": "prepared/motion.mp4",
                        "audio": "prepared/audio.wav",
                        "manifest": "prepared/manifest.json",
                    },
                )
                session.commit()
            response = client.post(
                f"/projects/{project_id}/approve",
                json={"stage": "performance_inputs", "approved_by": "offline owner"},
            )
            assert response.status_code == 200, response.text
            yield {
                "client": client,
                "runtime": runtime,
                "project_id": project_id,
                "source": source,
                "motion": motion,
                "audio": audio,
                "manifest": manifest,
                "template": template,
            }
    finally:
        runtime.close()


def post_plan(harness: dict[str, Any], body: dict[str, Any]):
    return harness["client"].post(
        f"/projects/{harness['project_id']}/performance/segment-plan", json=body
    )


def test_plan_uses_current_preparation_and_frozen_workflow_without_mutation(segment_plan_api):
    harness = segment_plan_api
    # The saved node snapshot, rather than the mutable exported file, is authoritative.
    harness["template"].write_text("MUTATED EXTERNAL EXPORT, NOT VALID JSON", encoding="utf-8")
    before_state = state_snapshot(harness["runtime"])
    before_files = input_files_snapshot(harness)
    response = post_plan(harness, {"duration_s": 30})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stage"] == "performance_interval_plan"
    assert result["source"]["sha256"] == digest(harness["source"].read_bytes())
    assert result["source_interval"]["start_s"] == 2.5
    assert result["source_interval"]["end_s"] == 32.5
    assert result["source_interval"]["duration_s"] == 30
    assert result["native_clock"]["frame_rate"] == "16/1"
    assert result["native_clock"]["frame_count"] == 480
    assert result["native_clock"]["sample_count"] == 1440000
    assert len(result["segments"]) == 3
    assert result["all_segments_match_exact_frame_count"] is True
    assert result["execution_available"] is False
    assert result["model_generated"] is False
    assert result["quality_verified"] is False
    assert post_plan(harness, {"duration_s": 30}).json() == result
    assert state_snapshot(harness["runtime"]) == before_state
    assert input_files_snapshot(harness) == before_files
    assert harness["runtime"].queue.list(harness["project_id"]) == []


def test_plan_reports_explicit_context_without_enabling_execution(segment_plan_api):
    harness = segment_plan_api
    before = state_snapshot(harness["runtime"])
    response = post_plan(harness, {"duration_s": 30, "context_frames": 8})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["workflow"]["context_frames"] == 8
    assert result["segments"][1]["input_context_frames"] == 8
    assert result["execution_available"] is False
    assert state_snapshot(harness["runtime"]) == before


@pytest.mark.parametrize(
    "defect",
    [
        "missing_preparation",
        "dirty_preparation",
        "source_replaced",
        "manifest_source_hash",
        "manifest_request",
        "manifest_invalid_json",
        "legacy_output_paths_list",
        "missing_workflow",
        "workflow_hash",
    ],
)
def test_plan_rejects_untrusted_or_stale_inputs_without_mutation(segment_plan_api, defect):
    harness = segment_plan_api
    with harness["runtime"].session_factory() as session:
        project = session.get(Project, harness["project_id"])
        prepare = next(node for node in project.nodes if node.kind == NodeKind.PERFORMANCE_PREPARE)
        motion = next(node for node in project.nodes if node.kind == NodeKind.MOTION_VIDEO)
        if defect == "missing_preparation":
            prepare.current_version = None
            prepare.current_version_id = None
        elif defect == "dirty_preparation":
            prepare.status = NodeStatus.DIRTY
        elif defect == "source_replaced":
            harness["source"].write_bytes(b"OFFLINE REPLACED SOURCE")
        elif defect == "legacy_output_paths_list":
            assert prepare.current_version is not None
            previous = prepare.current_version
            # Emulate an older list-shaped record while preserving the immutable history.
            legacy = Version(
                node_id=prepare.id,
                number=previous.number + 1,
                recipe_snapshot=deepcopy(previous.recipe_snapshot),
                output_paths=[],
                recipe_hash=previous.recipe_hash,
                cost=0,
                note="Offline legacy list-shaped output fixture",
            )
            session.add(legacy)
            session.flush()
            prepare.current_version = legacy
            prepare.current_version_id = legacy.id
        elif defect.startswith("manifest_"):
            manifest = json.loads(harness["manifest"].read_text(encoding="utf-8"))
            if defect == "manifest_source_hash":
                manifest["source"]["sha256"] = "f" * 64
            elif defect == "manifest_request":
                manifest["request"]["start_s"] = 3.5
            harness["manifest"].write_text(
                "NOT JSON" if defect == "manifest_invalid_json" else json.dumps(manifest),
                encoding="utf-8",
            )
        elif defect == "missing_workflow":
            motion.recipe = {**motion.recipe, "workflow": None}
        elif defect == "workflow_hash":
            workflow = deepcopy(motion.recipe["workflow"])
            workflow["template_sha256"] = "f" * 64
            motion.recipe = {**motion.recipe, "workflow": workflow}
        session.commit()
    before = state_snapshot(harness["runtime"])
    before_files = input_files_snapshot(harness)
    response = post_plan(harness, {"duration_s": 30})
    assert response.status_code == 409, response.text
    assert state_snapshot(harness["runtime"]) == before
    assert input_files_snapshot(harness) == before_files
    assert harness["runtime"].queue.list(harness["project_id"]) == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"duration_s": 0},
        {"duration_s": -1},
        {"duration_s": "30"},
        {"duration_s": True},
        {"duration_s": 30, "context_frames": -1},
        {"duration_s": 30, "context_frames": "8"},
        {"duration_s": 30, "context_frames": True},
        {"duration_s": 30, "unknown": 1},
    ],
)
def test_plan_rejects_invalid_request_parameters_without_mutation(segment_plan_api, body):
    harness = segment_plan_api
    before = state_snapshot(harness["runtime"])
    response = post_plan(harness, body)
    assert response.status_code == 422, response.text
    assert state_snapshot(harness["runtime"]) == before


@pytest.mark.parametrize("body", [{"duration_s": 70}, {"duration_s": 30, "context_frames": 160}])
def test_plan_rejects_unplannable_source_or_context_without_jobs(segment_plan_api, body):
    harness = segment_plan_api
    before = state_snapshot(harness["runtime"])
    response = post_plan(harness, body)
    assert response.status_code == 409, response.text
    assert state_snapshot(harness["runtime"]) == before
    assert harness["runtime"].queue.list(harness["project_id"]) == []
