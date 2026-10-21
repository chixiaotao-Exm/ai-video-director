"""Offline workflow protocol fixtures; these do not validate a real model."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from avd.backends.comfyui import ComfyUIClient, ComfyUIError, ComfyUITimeoutError
from avd.backends.workflows import WorkflowTemplateError
from avd.jobs.native import native_execution_observer
from avd.performance import motion
from avd.performance.motion import (
    MotionWorkflowSpec,
    motion_workflow_readiness,
    run_motion_workflow,
    snapshot_motion_workflow,
)
from avd.performance.prepare import PerformanceMediaError


def workflow_fixture(tmp_path: Path) -> tuple[dict, MotionWorkflowSpec]:
    template = {
        "1": {"class_type": "OfflineFixtureVideoInput", "inputs": {"file": "{{motion_video}}"}},
        "2": {
            "class_type": "OfflineFixtureReferenceInput",
            "inputs": {"file": "{{reference_image}}"},
        },
        "9": {
            "class_type": "OfflineFixtureVideoOutput",
            "inputs": {"seed": "{{seed}}", "video": ["1", 0]},
        },
    }
    path = tmp_path / "user-export.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    return template, MotionWorkflowSpec(
        template_path=path,
        revision="offline-fixture-v1",
        model_versions={"fixture-model": "test-only"},
        output_node_id="9",
    )


def timing(times: list[float], duration: float = 1) -> dict:
    return {
        "width": 32,
        "height": 32,
        "frame_count": len(times),
        "frame_timestamps_s": times,
        "duration_s": duration,
        "time_base": "1/10000",
        "tick_s": 0.0001,
    }


def test_export_snapshot_freezes_template_and_real_revision_declarations(tmp_path: Path) -> None:
    template, spec = workflow_fixture(tmp_path)
    snapshot = snapshot_motion_workflow(spec, "motion_video")
    spec.template_path.write_text("{}", encoding="utf-8")
    assert snapshot["template"] == template
    assert snapshot["spec"]["revision"] == "offline-fixture-v1"
    assert snapshot["spec"]["model_versions"] == {"fixture-model": "test-only"}
    assert snapshot["placeholders"] == ["motion_video", "reference_image", "seed"]
    assert len(snapshot["template_sha256"]) == len(snapshot["template_file_sha256"]) == 64


@pytest.mark.parametrize(
    "defect", ["stub", "embedded", "missing-input", "ui-export", "missing-output"]
)
def test_unusable_exports_fail_before_model_work(tmp_path: Path, defect: str) -> None:
    template, spec = workflow_fixture(tmp_path)
    if defect == "stub":
        template["1"]["class_type"] = "REPLACE_WITH_EXPORTED_WAN_NODE"
    elif defect == "embedded":
        template["1"]["inputs"]["file"] = "prefix/{{motion_video}}"
    elif defect == "missing-input":
        template["2"]["inputs"]["file"] = "hardcoded.png"
    elif defect == "ui-export":
        template = {"nodes": []}
    else:
        spec.output_node_id = "absent"
    spec.template_path.write_text(json.dumps(template), encoding="utf-8")
    with pytest.raises(WorkflowTemplateError):
        snapshot_motion_workflow(spec, "motion_video")


def test_comfy_protocol_upload_submit_download_and_evidence_are_explicit_offline_fakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, spec = workflow_fixture(tmp_path)
    snapshot = snapshot_motion_workflow(spec, "motion_video")
    (tmp_path / "source.mp4").write_bytes(b"OFFLINE INPUT FIXTURE, NOT A GENERATED VIDEO")
    (tmp_path / "reference.png").write_bytes(b"OFFLINE IMAGE FIXTURE")
    submitted = []
    uploaded = []
    execution_events = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/upload/image":
            assert execution_events == []
            uploaded.append(request.content)
            return httpx.Response(200, json={"name": f"input-{len(uploaded)}.bin"})
        if request.url.path == "/prompt":
            assert len(execution_events) == 1
            submitted.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "offline-prompt"})
        if request.url.path == "/history/offline-prompt":
            return httpx.Response(
                200,
                json={
                    "offline-prompt": {
                        "status": {"completed": True},
                        "outputs": {
                            "9": {"videos": [{"filename": "result.mp4", "type": "output"}]},
                            "2": {"images": [{"filename": "preview.png", "type": "output"}]},
                        },
                    }
                },
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"EXPLICIT OFFLINE PROTOCOL OUTPUT FIXTURE")
        pytest.fail(f"Unexpected mocked ComfyUI request: {request.method} {request.url.path}")

    monkeypatch.setattr(motion, "_video_timing", lambda path, tools: timing([0, 0.3, 0.6]))
    with (
        ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(handle)) as client,
        native_execution_observer(execution_events.append),
    ):
        result = run_motion_workflow(
            stage="motion_video",
            data_dir=tmp_path,
            workflow_snapshot=snapshot,
            inputs={"motion_video": "source.mp4", "reference_image": "reference.png"},
            output_dir=tmp_path / "output",
            client=client,
            parameters={"seed": 9},
        )
    assert len(uploaded) == 2 and len(submitted) == 1
    event = execution_events[0]
    assert event["source"] == "actual" and event["phase"] == "submit"
    assert event["details"]["workflow_sha256"] == snapshot["template_sha256"]
    assert event["details"]["declared_model_versions"] == spec.model_versions
    assert event["details"]["remote_weights_verified"] is False
    assert submitted[0]["1"]["inputs"]["file"] == "input-1.bin"
    assert submitted[0]["2"]["inputs"]["file"] == "input-2.bin"
    assert submitted[0]["9"]["inputs"]["seed"] == 9
    assert result["video"].name == "result.mp4"
    evidence = json.loads(result["manifest"].read_text(encoding="utf-8"))
    assert evidence["quality_verified"] is False
    assert evidence["workflow"] == snapshot
    assert evidence["frame_timing_policy"] == "source_frame_timestamps"
    assert evidence["inputs"]["motion_video"]["path"] == "source.mp4"
    assert "{{motion_video}}" in json.dumps(snapshot["template"])


@pytest.mark.parametrize("defect", ["template-hash", "missing-file-input", "unused-parameter"])
def test_invalid_snapshot_or_inputs_do_not_contact_comfyui(tmp_path: Path, defect: str) -> None:
    _, spec = workflow_fixture(tmp_path)
    snapshot = snapshot_motion_workflow(spec, "motion_video")
    inputs = {"motion_video": "source.mp4", "reference_image": "reference.png"}
    parameters = {"seed": 9}
    if defect == "template-hash":
        snapshot["template"]["9"]["inputs"]["seed"] = 11
    elif defect == "missing-file-input":
        inputs.pop("reference_image")
    else:
        parameters["unused"] = 1

    def unexpected(request):
        pytest.fail("Invalid workflow must fail before any network request")

    with ComfyUIClient(
        "http://127.0.0.1:8188", transport=httpx.MockTransport(unexpected)
    ) as client:
        with pytest.raises(WorkflowTemplateError):
            run_motion_workflow(
                stage="motion_video",
                data_dir=tmp_path,
                workflow_snapshot=snapshot,
                inputs=inputs,
                output_dir=tmp_path / "output",
                client=client,
                parameters=parameters,
            )


@pytest.mark.parametrize(
    "output,expected",
    [
        (timing([0, 0.3], 1), None),
        (timing([0, 0.3, 0.6], 0.9), None),
        (timing([0, 0.4, 0.6], 1), None),
        (timing([0.1, 0.4, 0.7], 1), None),
        (timing([0, 0.25, 0.5, 0.75], 1), None),
        (timing([0, 0.3, 0.5, 0.75], 1), 4),
    ],
)
def test_output_cannot_hide_missing_frames_duration_or_pts(
    output: dict, expected: int | None
) -> None:
    with pytest.raises(PerformanceMediaError):
        motion._check_timeline(timing([0, 0.3, 0.6]), output, expected)


def test_native_cfr_needs_explicit_frame_count_and_keeps_input_duration() -> None:
    assert (
        motion._check_timeline(timing([0, 0.3, 0.6]), timing([0, 0.25, 0.5, 0.75]), 4)
        == "native_constant_frame_rate"
    )


def test_selected_output_node_must_be_unambiguous() -> None:
    history = {"outputs": {"9": {"videos": [{"filename": "a.mp4"}, {"filename": "b.mp4"}]}}}
    with pytest.raises(WorkflowTemplateError, match="exactly one"):
        motion._output_filename(history, "9")
    duplicate = deepcopy(history)
    duplicate["outputs"]["9"]["videos"] = [{"filename": "a.mp4"}]
    duplicate["outputs"]["10"] = {"videos": [{"filename": "a.mp4"}]}
    with pytest.raises(WorkflowTemplateError, match="ambiguous"):
        motion._output_filename(duplicate, "9")


@pytest.mark.parametrize("defect", [None, "missing-node", "bad-model", "unreachable", "malformed"])
def test_readiness_reads_node_and_model_options_without_submitting(tmp_path, defect):
    template, spec = workflow_fixture(tmp_path)
    template["9"]["inputs"]["model"] = "declared-model.safetensors"
    spec.template_path.write_text(json.dumps(template), encoding="utf-8")
    snapshot = snapshot_motion_workflow(spec, "motion_video")
    calls = []

    def handle(request):
        calls.append(request.url.path)
        assert request.method == "GET" and request.url.path == "/object_info"
        if defect == "unreachable":
            return httpx.Response(503)
        if defect == "malformed":
            return httpx.Response(200, json=[])
        inventory = {node["class_type"]: {"input": {}} for node in template.values()}
        if defect == "missing-node":
            inventory.pop("OfflineFixtureVideoInput")
        inventory["OfflineFixtureVideoOutput"]["input"] = {
            "required": {
                "model": [
                    [
                        "other-model.safetensors"
                        if defect == "bad-model"
                        else "declared-model.safetensors"
                    ],
                    {},
                ]
            }
        }
        return httpx.Response(200, json=inventory)

    state = motion_workflow_readiness(
        snapshot,
        stage="motion_video",
        parameters={"seed": 9},
        profile={"base_url": "http://127.0.0.1:8188", "timeout": 1},
        transport=httpx.MockTransport(handle),
    )
    assert state["ready"] is (defect is None)
    assert state["configured"]
    assert not state["quality_verified"] and not state["model_versions_verified"]
    assert bool(state["reasons"]) is (defect is not None)
    assert calls == ["/object_info"]


def test_retry_resumes_durable_prompt_without_reupload_or_resubmit(tmp_path, monkeypatch):
    _, spec = workflow_fixture(tmp_path)
    snapshot = snapshot_motion_workflow(spec, "motion_video")
    (tmp_path / "source.mp4").write_bytes(b"OFFLINE INPUT")
    (tmp_path / "reference.png").write_bytes(b"OFFLINE IMAGE")
    monkeypatch.setattr(motion, "_video_timing", lambda path, tools: timing([0, 0.3, 0.6]))
    calls = []
    execution_events = []
    finished = False

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.bin"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "durable-offline-prompt"})
        if request.url.path == "/history/durable-offline-prompt":
            return httpx.Response(
                200,
                json={
                    "durable-offline-prompt": {
                        "status": {"completed": True},
                        "outputs": {
                            "9": {"videos": [{"filename": "result.mp4", "type": "output"}]}
                        },
                    }
                }
                if finished
                else {},
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"EXPLICIT OFFLINE OUTPUT")
        pytest.fail(str(request.url))

    common = {
        "stage": "motion_video",
        "data_dir": tmp_path,
        "workflow_snapshot": snapshot,
        "inputs": {"motion_video": "source.mp4", "reference_image": "reference.png"},
        "parameters": {"seed": 9},
        "journal_path": tmp_path / "job" / "submission.json",
    }
    with (
        ComfyUIClient(
            "http://127.0.0.1:8188", timeout=0.001, transport=httpx.MockTransport(handle)
        ) as client,
        native_execution_observer(execution_events.append),
    ):
        with pytest.raises(ComfyUITimeoutError):
            run_motion_workflow(**common, output_dir=tmp_path / "attempt-1", client=client)
    saved = json.loads(common["journal_path"].read_text(encoding="utf-8"))
    assert saved["prompt_id"] == "durable-offline-prompt"
    finished = True
    with (
        ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(handle)) as client,
        native_execution_observer(execution_events.append),
    ):
        result = run_motion_workflow(**common, output_dir=tmp_path / "attempt-2", client=client)
    assert calls.count(("POST", "/prompt")) == 1
    assert calls.count(("POST", "/upload/image")) == 2
    assert [event["source"] for event in execution_events] == ["actual", "resumed"]
    assert [event["phase"] for event in execution_events] == ["submit", "resume"]
    assert execution_events[-1]["details"]["prompt_id"] == "durable-offline-prompt"
    assert json.loads(result["manifest"].read_text(encoding="utf-8"))["submission_reused"]


def test_unknown_submit_outcome_blocks_automatic_duplicate(tmp_path, monkeypatch):
    _, spec = workflow_fixture(tmp_path)
    (tmp_path / "source.mp4").write_bytes(b"OFFLINE INPUT")
    (tmp_path / "reference.png").write_bytes(b"OFFLINE IMAGE")
    monkeypatch.setattr(motion, "_video_timing", lambda path, tools: timing([0, 0.3, 0.6]))
    calls = []

    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/upload/image":
            return httpx.Response(200, json={"name": "uploaded.bin"})
        raise httpx.ReadTimeout("submission response lost", request=request)

    common = {
        "stage": "motion_video",
        "data_dir": tmp_path,
        "workflow_snapshot": snapshot_motion_workflow(spec, "motion_video"),
        "inputs": {"motion_video": "source.mp4", "reference_image": "reference.png"},
        "parameters": {"seed": 9},
        "journal_path": tmp_path / "job" / "submission.json",
    }
    with ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ComfyUIError):
            run_motion_workflow(**common, output_dir=tmp_path / "attempt-1", client=client)
        with pytest.raises(WorkflowTemplateError, match="unknown outcome"):
            run_motion_workflow(**common, output_dir=tmp_path / "attempt-2", client=client)
    assert calls == ["/upload/image", "/upload/image", "/prompt"]


@pytest.mark.parametrize("limit", ["max_duration_s", "max_input_frames"])
def test_declared_model_limits_fail_before_upload(tmp_path, monkeypatch, limit):
    _, spec = workflow_fixture(tmp_path)
    setattr(spec, limit, 0.5 if limit == "max_duration_s" else 2)
    (tmp_path / "source.mp4").write_bytes(b"OFFLINE INPUT")
    (tmp_path / "reference.png").write_bytes(b"OFFLINE IMAGE")
    monkeypatch.setattr(motion, "_video_timing", lambda path, tools: timing([0, 0.3, 0.6]))
    with ComfyUIClient(
        "http://127.0.0.1:8188",
        transport=httpx.MockTransport(
            lambda request: pytest.fail("Model limit must be checked before network work")
        ),
    ) as client:
        with pytest.raises(PerformanceMediaError, match="limit"):
            run_motion_workflow(
                stage="motion_video",
                data_dir=tmp_path,
                workflow_snapshot=snapshot_motion_workflow(spec, "motion_video"),
                inputs={"motion_video": "source.mp4", "reference_image": "reference.png"},
                parameters={"seed": 9},
                output_dir=tmp_path / "output",
                client=client,
            )
