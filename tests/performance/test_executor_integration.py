"""Exercise the production job contracts with real local media and explicit fake model audio."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from avd.config import Settings
from avd.executors import build_registry
from avd.jobs.models import Job
from avd.performance import MediaTools, PreparationRequest, prepare_performance
from avd.performance import executors as performance_executors


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch):
    bundle = Path(__file__).resolve().parents[2] / "data/tools/ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip("Real FFmpeg tools required")
    tools = MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)
    monkeypatch.setattr("avd.performance.executors.media_tools", lambda root: tools)
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:size=384x256:rate=10:duration=1.2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.2",
            "-c:v",
            "libx264",
            "-c:a",
            "pcm_s16le",
            str(tmp_path / "reference.mkv"),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    prepared = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path="reference.mkv", duration_s=1.2),
        output_relative="prepared",
        tools=tools,
    )
    dependencies = {
        "prepare": {
            "kind": "performance_prepare",
            "outputs": {
                "manifest": "prepared/manifest.json",
                "motion": prepared.motion.path,
                "audio": prepared.audio.path,
            },
            "recipe": {"source_sha256": prepared.source.sha256},
        },
        "stems": {
            "kind": "vocal_separation",
            "outputs": {
                "accompaniment": prepared.audio.path,
            },
            "recipe": {},
        },
        # A real tone stands in for model output only in this offline contract test.
        "singing": {
            "kind": "singing_voice",
            "outputs": {
                "vocals": prepared.audio.path,
            },
            "recipe": {},
        },
        "motion": {
            "kind": "motion_video",
            "outputs": {
                "video": prepared.motion.path,
            },
            "recipe": {},
        },
    }
    return tmp_path, prepared, dependencies


def job(root, dependencies, stage, recipe):
    return Job(
        lane="cpu",
        job_type=stage,
        attempts=1,
        payload={
            "data_dir": str(root),
            "output_dir": f"projects/offline-contract/{stage}",
            "recipe": recipe,
            "inputs": dependencies,
            "context": {},
        },
    )


def test_real_handlers_mix_sync_export_owned_outputs_and_capture_settings(inputs):
    root, prepared, dependencies = inputs
    registry = build_registry(Settings(_env_file=None, data_dir=root, ai_label=False))
    mix = registry.get("performance_mix")(
        job(
            root,
            dependencies,
            "performance_mix",
            {"vocal_gain": 0.5, "accompaniment_gain": 0.25},
        )
    )
    manifest = json.loads((root / mix["outputs"]["manifest"]).read_text(encoding="utf-8"))
    assert manifest["parameters"]["vocal_gain_db"] == pytest.approx(-6.020599913)
    assert mix["duration_s"] == prepared.timeline.actual_duration_s
    dependencies["mix"] = {"kind": "performance_mix", "outputs": mix["outputs"], "recipe": {}}
    sync = registry.get("performance_sync")(job(root, dependencies, "performance_sync", {}))
    assert sync["outputs"]["video"] != prepared.motion.path
    assert (root / sync["outputs"]["video"]).read_bytes() == (
        root / prepared.motion.path
    ).read_bytes()
    dependencies["sync"] = {"kind": "performance_sync", "outputs": sync["outputs"], "recipe": {}}
    final = registry.get("performance_final")(
        job(
            root,
            dependencies,
            "performance_final",
            {"lipsync": False, "_execution": {"ai_label": True}},
        )
    )
    report = json.loads((root / final["outputs"]["manifest"]).read_text(encoding="utf-8"))
    assert report["ai_label"] is True
    assert report["output_frame_count"] == prepared.timeline.frame_count
    assert all((root / output).is_file() for output in final["outputs"].values())
    tools = performance_executors.media_tools(root)
    metadata = subprocess.run(
        [
            tools.ffprobe,
            "-v",
            "error",
            "-show_format",
            "-of",
            "json",
            str(root / final["outputs"]["video"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(metadata.stdout)["format"]["tags"]["comment"] == "AI-generated"

    def white_pixels(path):
        frame = subprocess.run(
            [
                tools.ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        return sum(min(frame[index : index + 3]) > 160 for index in range(0, len(frame), 3))

    # The input is solid green. Real decoded white text proves the burn-in,
    # independently of the manifest and container comment.
    assert white_pixels(root / prepared.motion.path) == 0
    assert white_pixels(root / final["outputs"]["video"]) > 0


@pytest.mark.parametrize("execution", [{}, {"ai_label": False}])
def test_final_refuses_legacy_unlabelled_jobs_without_rewriting_their_recipe(tmp_path, execution):
    registry = build_registry(Settings(_env_file=None, data_dir=tmp_path, ai_label=True))
    queued = job(tmp_path, {}, "performance_final", {"_execution": execution})
    with pytest.raises(ValueError, match="frozen AI-generated label"):
        registry.get("performance_final")(queued)
    assert queued.payload["recipe"]["_execution"] == execution
    assert not (tmp_path / "projects/offline-contract/performance_final").exists()


def test_real_sync_rejects_a_falsely_declared_native_frame_count(inputs):
    root, _, dependencies = inputs
    dependencies["motion"]["recipe"]["expected_frame_count"] = 13
    registry = build_registry(Settings(_env_file=None, data_dir=root))
    with pytest.raises(RuntimeError, match="frame count mismatch"):
        registry.get("performance_sync")(job(root, dependencies, "performance_sync", {}))
    assert not (root / "projects/offline-contract/performance_sync/attempt-1").exists()
