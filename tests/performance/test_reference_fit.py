"""Real CPU image fitting with offline ComfyUI replies; no model or network calls."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from avd.backends.comfyui import ComfyUIClient, ComfyUITimeoutError
from avd.backends.workflows import WorkflowTemplateError
from avd.performance import motion, reference_fit
from avd.performance.prepare import MediaTools, PerformanceMediaError, _hash
from avd.performance.reference_fit import fit_reference_image
from avd.performance.workflow_spec import MotionWorkflowSpec, ReferenceImageFit


@pytest.fixture
def media_tools() -> MediaTools:
    bundle = Path(__file__).resolve().parents[2] / "data" / "tools" / "ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
        pytest.skip("Real local FFmpeg/ffprobe are unavailable")
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe, timeout_s=30)


@pytest.mark.parametrize(
    "patch",
    [
        {"width": 0},
        {"width": -1},
        {"width": True},
        {"width": 96.0},
        {"height": "128"},
        {"height": 0},
        {"padding_color": "navy"},
        {"padding_color": "#123"},
        {"padding_color": "#GG0000"},
        {"padding_color": " #001122"},
        {"padding_color": "#001122\n"},
        {"padding_color": "#001122:crop=1"},
        {"crop": True},
    ],
)
def test_fit_contract_accepts_only_positive_integer_canvas_and_exact_hex_color(patch):
    with pytest.raises(ValidationError):
        ReferenceImageFit.model_validate({"width": 96, "height": 128, **patch})


def _reference(path: Path, width: int, height: int) -> None:
    pixels = bytearray()
    for y in range(height):
        color = (
            (240, 25, 10)
            if y < max(1, height // 8)
            else (10, 235, 30)
            if y >= height - max(1, height // 8)
            else (100, 120, 140)
        )
        pixels.extend(bytes(color) * width)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)


def _rgb(path: Path, tools: MediaTools) -> bytes:
    return subprocess.run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout


@pytest.mark.parametrize(
    "source_size,canvas",
    [((80, 240), (96, 128)), ((240, 80), (96, 128)), ((60, 80), (96, 128)), ((81, 233), (95, 127))],
)
def test_real_ffmpeg_preserves_head_and_feet_and_centers_whole_image(
    tmp_path, media_tools, source_size, canvas
):
    original = tmp_path / "original-stage.ppm"
    _reference(original, *source_size)
    before = original.read_bytes()
    contract = ReferenceImageFit(width=canvas[0], height=canvas[1])
    fitted, record = fit_reference_image(
        data_dir=tmp_path,
        source=original,
        source_sha256=_hash(original),
        destination=tmp_path / "attempt-1" / "reference.png",
        contract=contract,
        tools=media_tools,
    )
    assert original.read_bytes() == before and fitted != original
    assert record["source"]["sha256"] == _hash(original)
    assert record["fitted"]["sha256"] == _hash(fitted)
    assert record["fitted"]["width"] == canvas[0]
    assert record["fitted"]["height"] == canvas[1]
    assert not record["cropped"] and not record["stretched"]
    placement = record["placement"]
    assert placement["x"] == (canvas[0] - placement["width"]) // 2
    assert placement["y"] == (canvas[1] - placement["height"]) // 2
    assert abs(placement["width"] / source_size[0] - placement["height"] / source_size[1]) <= max(
        1 / source_size[0], 1 / source_size[1]
    )
    decoded = _rgb(fitted, media_tools)
    assert len(decoded) == canvas[0] * canvas[1] * 3

    def pixel(x, y):
        start = (y * canvas[0] + x) * 3
        return tuple(decoded[start : start + 3])

    center = placement["x"] + placement["width"] // 2
    head = pixel(center, placement["y"] + max(1, placement["height"] // 20))
    feet = pixel(
        center, placement["y"] + placement["height"] - 1 - max(1, placement["height"] // 20)
    )
    assert head[0] > 200 and head[1] < 60
    assert feet[1] > 200 and feet[0] < 60
    padding = (11, 18, 32)
    if placement["x"]:
        assert pixel(0, canvas[1] // 2) == padding
        assert pixel(canvas[0] - 1, canvas[1] // 2) == padding
    if placement["y"]:
        assert pixel(canvas[0] // 2, 0) == padding
        assert pixel(canvas[0] // 2, canvas[1] - 1) == padding


def _workflow(tmp_path, *, fit=True):
    template = tmp_path / "offline-template.json"
    template.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflineFixture",
                    "inputs": {"motion": "{{motion_video}}", "image": "{{reference_image}}"},
                }
            }
        ),
        encoding="utf-8",
    )
    return MotionWorkflowSpec(
        template_path=template,
        revision="reference-fit-offline-fixture",
        model_versions={"offline-fixture": "not-a-real-model"},
        output_node_id="9",
        reference_image_fit=ReferenceImageFit(width=96, height=128) if fit else None,
    )


@pytest.fixture
def submitted_reference(tmp_path, media_tools, monkeypatch):
    _reference(tmp_path / "original-stage.ppm", 80, 240)
    (tmp_path / "source.mp4").write_bytes(b"EXPLICIT OFFLINE MOTION INPUT")
    monkeypatch.setattr(
        motion,
        "_video_timing",
        lambda path, tools: {
            "width": 32,
            "height": 32,
            "frame_count": 3,
            "frame_timestamps_s": [0, 0.3, 0.6],
            "duration_s": 1,
            "time_base": "1/10000",
            "tick_s": 0.0001,
        },
    )
    state = {"finished": False, "requests": [], "fit_encodes": 0}
    real_run = reference_fit._run

    def tracked(arguments, tools):
        if "-vf" in arguments:
            state["fit_encodes"] += 1
        return real_run(arguments, tools)

    monkeypatch.setattr(reference_fit, "_run", tracked)

    def transport(request):
        state["requests"].append(request.url.path)
        if request.url.path == "/upload/image":
            if b"fitted-reference-input.png" in request.content:
                assert b"\x89PNG\r\n\x1a\n" in request.content
                assert b"P6\n80 240" not in request.content
            return httpx.Response(200, json={"name": "offline-upload.bin"})
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "offline-fitted-reference"})
        if request.url.path == "/history/offline-fitted-reference":
            return httpx.Response(
                200,
                json={
                    "offline-fitted-reference": {
                        "status": {"completed": True},
                        "outputs": {"9": {"videos": [{"filename": "result.mp4"}]}},
                    }
                }
                if state["finished"]
                else {},
            )
        if request.url.path == "/view":
            return httpx.Response(200, content=b"EXPLICIT OFFLINE GENERATED OUTPUT FIXTURE")
        pytest.fail(f"Unexpected offline request: {request.url.path}")

    common = {
        "stage": "motion_video",
        "data_dir": tmp_path,
        "workflow_snapshot": motion.snapshot_motion_workflow(_workflow(tmp_path), "motion_video"),
        "inputs": {"motion_video": "source.mp4", "reference_image": "original-stage.ppm"},
        "journal_path": tmp_path / "job" / "remote-submission.json",
        "tools": media_tools,
    }
    with ComfyUIClient(
        "http://127.0.0.1:8188", timeout=0.001, transport=httpx.MockTransport(transport)
    ) as client:
        with pytest.raises(ComfyUITimeoutError):
            motion.run_motion_workflow(**common, output_dir=tmp_path / "attempt-1", client=client)
    saved = json.loads(common["journal_path"].read_text(encoding="utf-8"))
    assert saved["reference_image_fit"]["source"]["path"] == "original-stage.ppm"
    yield common, saved, state, transport


def test_resume_reuses_verified_fitted_copy_and_keeps_input_and_output_manifests(
    tmp_path, submitted_reference
):
    common, saved, state, transport = submitted_reference
    state["finished"] = True
    with ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(transport)) as client:
        result = motion.run_motion_workflow(
            **common, output_dir=tmp_path / "attempt-2", client=client
        )
    first = json.loads((tmp_path / "attempt-1/native_input_manifest.json").read_text())
    resumed = json.loads((tmp_path / "attempt-2/native_input_manifest.json").read_text())
    final = json.loads(result["manifest"].read_text())
    assert state["fit_encodes"] == 1
    assert state["requests"].count("/prompt") == 1
    assert state["requests"].count("/upload/image") == 2
    assert final["submission_reused"]
    assert (
        final["reference_image_fit"] == first["reference_image_fit"] == saved["reference_image_fit"]
    )
    assert resumed["reference_image_fit"] == saved["reference_image_fit"]
    assert final["inputs"]["reference_image"]["path"] == "original-stage.ppm"
    assert (
        final["effective_inputs"]["reference_image"] == first["effective_inputs"]["reference_image"]
    )
    assert final["effective_inputs"]["reference_image"]["path"].startswith("attempt-1/")
    assert not (tmp_path / "attempt-2/fitted-reference-input.png").exists()


@pytest.mark.parametrize("defect", ["source", "copy", "placement", "contract", "fingerprint"])
def test_changed_fit_inputs_or_evidence_fail_before_resuming_or_submitting(
    tmp_path, submitted_reference, defect
):
    common, saved, state, transport = submitted_reference
    state["finished"] = True
    if defect == "source":
        _reference(tmp_path / "original-stage.ppm", 81, 240)
    elif defect == "copy":
        (tmp_path / saved["reference_image_fit"]["fitted"]["path"]).write_bytes(b"changed image")
    elif defect == "placement":
        saved["reference_image_fit"]["placement"]["x"] += 1
    elif defect == "contract":
        common["workflow_snapshot"]["spec"]["reference_image_fit"]["width"] = 100
    else:
        # The saved copy still verifies, but its bound evidence no longer matches
        # the original remote submission's fingerprint.
        saved["reference_image_fit"]["ffmpeg_version"] = "different fitting implementation evidence"
    if defect in {"placement", "fingerprint"}:
        common["journal_path"].write_text(json.dumps(saved), encoding="utf-8")
    requests_before = list(state["requests"])
    with ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(transport)) as client:
        with pytest.raises((PerformanceMediaError, WorkflowTemplateError)):
            motion.run_motion_workflow(**common, output_dir=tmp_path / "attempt-2", client=client)
    assert state["requests"] == requests_before and state["fit_encodes"] == 1


def test_undeclared_fitting_preserves_old_snapshot_contract(tmp_path):
    snapshot = motion.snapshot_motion_workflow(_workflow(tmp_path, fit=False), "motion_video")
    assert "reference_image_fit" not in snapshot["spec"]
    configured, _ = motion.validate_motion_snapshot(snapshot, "motion_video", {})
    assert configured.reference_image_fit is None


def test_fit_declaration_cannot_be_used_for_workflows_without_reference_input(tmp_path):
    configured = _workflow(tmp_path)
    configured.template_path.write_text(
        json.dumps(
            {
                "9": {
                    "class_type": "OfflineFixture",
                    "inputs": {"video": "{{motion_video}}", "audio": "{{converted_vocals}}"},
                }
            }
        )
    )
    snapshot = motion.snapshot_motion_workflow(configured, "performance_lipsync")
    with pytest.raises(WorkflowTemplateError, match="reference_image"):
        motion.validate_motion_snapshot(snapshot, "performance_lipsync", {})
