"""Offline media-step contracts and one real FFmpeg round trip."""

import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from avd.agents.analysis import media, steps
from avd.agents.analysis.models import MediaInfo, Segment, ShotSpan, StructureTemplate


def test_probe_reads_fractional_rate_and_container_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "streams": [
            {"codec_type": "audio"},
            {"codec_type": "video", "width": 640, "height": 360, "avg_frame_rate": "30000/1001"},
        ],
        "format": {"duration": "3.0"},
    }
    calls = []

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((arguments, kwargs))
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr(media.subprocess, "run", run)
    result = media.probe(Path("a video.mp4"))
    assert result.duration_s == 3
    assert result.width == 640
    assert result.fps == pytest.approx(29.97003)
    assert result.has_audio
    assert calls[0][0][-1] == "a video.mp4"
    assert "shell" not in calls[0][1]


@pytest.mark.parametrize("payload", ["garbage", "{}", '{"streams": []}', '{"streams": null}'])
def test_probe_rejects_invalid_video_metadata(
    monkeypatch: pytest.MonkeyPatch, payload: str
) -> None:
    monkeypatch.setattr(
        media.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=payload)
    )
    with pytest.raises(media.MediaProcessingError, match="metadata"):
        media.probe("bad.mp4")


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (FileNotFoundError(), "required on PATH"),
        (subprocess.CalledProcessError(1, ["ffprobe"], stderr="invalid data"), "invalid data"),
        (subprocess.TimeoutExpired(["ffprobe"], 120), "timed out"),
    ],
)
def test_probe_reports_tool_failures(
    monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(media.subprocess, "run", fail)
    with pytest.raises(media.MediaProcessingError, match=message):
        media.probe("bad.mp4")


def test_extract_audio_passes_safe_pcm_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "speech.wav"
    calls: list[list[str]] = []

    def run(arguments: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(arguments)
        output.write_bytes(b"fake wav")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(media.subprocess, "run", run)
    assert media.extract_audio("source with spaces.mp4", output) == output
    assert calls[0][calls[0].index("-i") + 1] == "source with spaces.mp4"
    assert calls[0][calls[0].index("-ar") + 1] == "16000"
    assert calls[0][calls[0].index("-ac") + 1] == "1"
    assert "pcm_s16le" in calls[0]
    assert "-n" in calls[0]
    with pytest.raises(FileExistsError, match="overwrite"):
        media.extract_audio("source.mp4", output)
    assert output.read_bytes() == b"fake wav"
    with pytest.raises(ValueError, match="differ"):
        media.extract_audio(output, output)


def test_extract_audio_rejects_missing_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(media.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=""))
    with pytest.raises(media.MediaProcessingError, match="did not produce"):
        media.extract_audio("source.mp4", tmp_path / "missing.wav")


def test_scene_detection_and_no_cut_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def timecode(seconds: float) -> SimpleNamespace:
        return SimpleNamespace(get_seconds=lambda: seconds)

    scene_module = SimpleNamespace(
        ContentDetector=lambda: "detector",
        detect=lambda path, detector: [(timecode(0), timecode(1)), (timecode(1), timecode(3))],
    )
    monkeypatch.setitem(sys.modules, "scenedetect", scene_module)
    assert steps.detect_shots("clip.mp4") == [
        ShotSpan(start_s=0, end_s=1),
        ShotSpan(start_s=1, end_s=3),
    ]
    scene_module.detect = lambda path, detector: []
    monkeypatch.setattr(
        steps,
        "probe",
        lambda path: MediaInfo(duration_s=3, width=16, height=16, fps=25, has_audio=False),
    )
    assert steps.detect_shots("clip.mp4") == [ShotSpan(start_s=0, end_s=3)]


def test_transcribe_uses_fake_sdk_and_model_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class FakeWhisper:
        def __init__(self, model_size: str, **kwargs: Any) -> None:
            calls.append((model_size, kwargs))

        def transcribe(self, path: str, **kwargs: Any) -> tuple[Any, None]:
            calls.append((path, kwargs))
            return iter(
                [
                    SimpleNamespace(start=0, end=1.2, text=" Hello "),
                    SimpleNamespace(start=1.2, end=1.3, text=" "),
                    SimpleNamespace(start=1.3, end=3, text="world"),
                ]
            ), None

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=FakeWhisper))
    monkeypatch.setattr(steps, "_read_pcm16", lambda path: f"samples:{path.name}")
    assert steps.transcribe("audio.wav", "tiny") == [
        Segment(start_s=0, end_s=1.2, text="Hello"),
        Segment(start_s=1.3, end_s=3, text="world"),
    ]
    assert calls[0] == ("tiny", {"device": "cpu", "compute_type": "int8"})
    assert calls[1][0] == "samples:audio.wav"


@pytest.mark.parametrize(
    ("module", "function"),
    [("scenedetect", steps.detect_shots), ("faster_whisper", steps.transcribe)],
)
def test_optional_dependencies_have_clear_errors(
    monkeypatch: pytest.MonkeyPatch, module: str, function: Any
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(RuntimeError, match="analysis.*extra"):
        function("input")


@pytest.mark.parametrize(
    ("start", "end"), [(2, 1), (1, 1), (-1, 2), (0, float("inf")), (float("nan"), 1)]
)
def test_time_spans_validate_finite_ordered_ranges(start: float, end: float) -> None:
    with pytest.raises(ValidationError):
        ShotSpan(start_s=start, end_s=end)


def test_structure_template_requires_shots_and_finite_duration() -> None:
    with pytest.raises(ValidationError):
        StructureTemplate(
            total_duration_s=float("inf"),
            hook="hook",
            shots=[],
            pacing_notes="fast",
            music_style="none",
            voice_style="neutral",
        )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and ffprobe required"
)
def test_ffmpeg_three_second_clip(tmp_path: Path) -> None:
    video = tmp_path / "three seconds.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    info = media.probe(video)
    assert info.duration_s == pytest.approx(3, abs=0.05)
    assert (info.width, info.height, info.fps, info.has_audio) == (160, 90, 25, True)
    audio = media.extract_audio(video)
    with wave.open(str(audio), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getframerate() == 16000
        assert stream.getnframes() / stream.getframerate() == pytest.approx(3, abs=0.1)
