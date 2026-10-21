"""Time provenance and real FFmpeg checks for the performance source interval."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from avd.performance import (
    MediaTools,
    PerformanceMediaError,
    PreparationRequest,
    prepare_performance,
)
from avd.performance import prepare as preparation
from avd.performance.models import PreparedPerformance


def _metadata(*, audio: bool = True) -> dict[str, Any]:
    streams: list[dict[str, Any]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "time_base": "1/90000",
            "start_pts": 270000,
            "duration": "5",
            "width": 640,
            "height": 360,
            "avg_frame_rate": "24000/1001",
            "r_frame_rate": "24/1",
            "nb_frames": "120",
            "side_data_list": [{"rotation": -90}],
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "pcm_s16le",
                "time_base": "1/48000",
                "start_pts": 168000,
                "start_time": "3.5",
                "duration": "4.5",
                "sample_rate": "48000",
                "channels": 2,
            }
        )
    return {"streams": streams, "format": {"start_time": "3", "duration": "5"}}


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source's clip.mp4"
    source.write_bytes(b"original reference")
    return source


def _fake_probe(monkeypatch: pytest.MonkeyPatch, data: Any) -> None:
    monkeypatch.setattr(
        preparation.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(data)),
    )


@pytest.mark.parametrize(
    "values",
    [
        {"source_video_path": "../reference.mp4"},
        {"source_video_path": "D:\\outside.mp4"},
        {"source_video_path": "/outside.mp4"},
        {"source_video_path": "reference.mp4", "duration_s": 0},
        {"source_video_path": "reference.mp4", "start_s": -1},
        {"source_video_path": "reference.mp4", "duration_s": float("inf")},
        {"source_video_path": "reference.mp4", "sample_rate": 0},
        {"source_video_path": "reference.mp4", "fps": 30},
    ],
)
def test_request_rejects_unsafe_paths_and_time_stretch_options(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PreparationRequest.model_validate(values)


def test_probe_keeps_source_hash_rotation_timebase_and_audio_offset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _source(tmp_path)
    before = source.read_bytes()
    _fake_probe(monkeypatch, _metadata())
    result = preparation.probe_source(tmp_path, source.name)
    assert result.sha256 == hashlib.sha256(before).hexdigest()
    assert result.video.start_pts == 270000
    assert result.video.start_time_s == 3
    assert result.video.time_base == "1/90000"
    assert result.video.rotation_degrees == -90
    assert result.video.avg_frame_rate == "24000/1001"
    assert result.audio is not None and result.audio.sample_rate == 48000
    assert result.audio_offset_s == 0.5
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "payload", [None, [], {"streams": []}, {"streams": None}, {"streams": ["invalid"]}]
)
def test_invalid_probe_metadata_is_actionable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: Any
) -> None:
    source = _source(tmp_path)
    _fake_probe(monkeypatch, payload)
    with pytest.raises(PerformanceMediaError):
        preparation.probe_source(tmp_path, source.name)


def test_no_audio_rejected_before_creating_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _source(tmp_path)
    _fake_probe(monkeypatch, _metadata(audio=False))
    with pytest.raises(PerformanceMediaError, match="no audio track"):
        prepare_performance(tmp_path, PreparationRequest(source_video_path=source.name))
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("times", [[0, 0], [1, 0], [None], []])
def test_unmappable_frames_rejected_before_creating_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, times: list[float | None]
) -> None:
    source = _source(tmp_path)

    def probe(path: Path, tools: MediaTools, **kwargs: Any) -> dict[str, Any]:
        if "frame_stream" in kwargs:
            return {"frames": [{"pts_time": value} for value in times]}
        return _metadata()

    monkeypatch.setattr(preparation, "_probe", probe)
    with pytest.raises(PerformanceMediaError, match="frame timing"):
        prepare_performance(tmp_path, PreparationRequest(source_video_path=source.name))
    assert list(tmp_path.iterdir()) == [source]


def test_interval_outside_source_rejected_before_creating_outputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = _source(tmp_path)
    _fake_probe(monkeypatch, _metadata())
    monkeypatch.setattr(preparation, "_frame_times", lambda *args: [3, 3.5, 4])
    with pytest.raises(PerformanceMediaError, match="no source video frames"):
        prepare_performance(tmp_path, PreparationRequest(source_video_path=source.name, start_s=50))
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("second_time", [3.52, 3.505])
def test_audio_timestamp_gaps_or_overlaps_do_not_silently_shift_song(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, second_time: float
) -> None:
    source = _source(tmp_path)

    def probe(path: Path, tools: MediaTools, **kwargs: Any) -> dict[str, Any]:
        if "frame_stream" in kwargs:
            return {
                "frames": [
                    {"pts_time": "3.5", "nb_samples": 480},
                    {"pts_time": str(second_time), "nb_samples": 480},
                ]
            }
        return _metadata()

    monkeypatch.setattr(preparation, "_probe", probe)
    monkeypatch.setattr(preparation, "_frame_times", lambda *args: [3, 3.1, 3.2])
    with pytest.raises(PerformanceMediaError, match="timestamp gaps or overlaps"):
        prepare_performance(tmp_path, PreparationRequest(source_video_path=source.name))
    assert list(tmp_path.iterdir()) == [source]


def test_output_traversal_is_rejected_before_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(
        preparation, "_probe", lambda *args, **kwargs: pytest.fail("unexpected probe")
    )
    with pytest.raises(ValueError, match="relative path"):
        prepare_performance(
            tmp_path,
            PreparationRequest(source_video_path=source.name),
            output_relative="../outside",
        )


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("ffprobe"),
        subprocess.TimeoutExpired("ffprobe", 10),
        subprocess.CalledProcessError(1, "ffprobe", stderr="unsupported container"),
    ],
)
def test_media_tool_errors_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    source = _source(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(preparation.subprocess, "run", fail)
    with pytest.raises(PerformanceMediaError):
        preparation.probe_source(tmp_path, source.name)
    assert list(tmp_path.iterdir()) == [source]


@pytest.fixture
def media_tools() -> MediaTools:
    bundle = Path(__file__).resolve().parents[2] / "data" / "tools" / "ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
        pytest.skip("FFmpeg and ffprobe are required for real timeline checks")
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def _generate(tools: MediaTools, path: Path, *, vfr: bool = False, audio_delay: float = 0) -> None:
    arguments = [
        tools.ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-n",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=96x64:rate=30:duration=2.4",
        "-itsoffset",
        str(audio_delay),
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2.4",
    ]
    if vfr:
        arguments += ["-vf", "select=not(mod(n\\,3))+eq(mod(n\\,7)\\,0)", "-fps_mode", "vfr"]
    arguments += [
        "-t",
        "2.4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "pcm_s16le",
        str(path),
    ]
    subprocess.run(arguments, check=True, capture_output=True, timeout=30)


@pytest.mark.parametrize("vfr", [False, True])
def test_real_preparation_preserves_frames_and_source_and_maps_all_anchors(
    tmp_path: Path, media_tools: MediaTools, vfr: bool
) -> None:
    source = tmp_path / "reference.mkv"
    _generate(media_tools, source, vfr=vfr)
    digest_before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path=source.name, start_s=0.21, duration_s=1.1),
        output_relative="prepared",
        tools=media_tools,
    )
    assert result.stage == "source_preparation"
    assert result.timeline.source_interval_start_pts_time_s >= 0.21
    assert result.timeline.actual_duration_s == pytest.approx(1.1, abs=0.11)
    assert result.motion.frame_count == result.timeline.frame_count
    assert result.motion.frame_count > 10
    assert result.audio.duration_s == pytest.approx(
        result.timeline.actual_duration_s, abs=1 / 48000
    )
    anchors = result.timeline.anchors
    for anchor in (anchors[0], anchors[len(anchors) // 2], anchors[-1]):
        assert result.timeline.source_time(anchor.output_time_s) == pytest.approx(
            anchor.source_pts_time_s, abs=result.timeline.frame_time_tolerance_s
        )
    if vfr:
        deltas = {round(b.output_time_s - a.output_time_s, 3) for a, b in zip(anchors, anchors[1:])}
        assert len(deltas) > 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest_before == result.source.sha256
    saved = PreparedPerformance.model_validate_json(
        (tmp_path / "prepared/manifest.json").read_text()
    )
    assert saved == result
    output = preparation.probe_source(tmp_path, result.motion.path, tools=media_tools)
    assert output.audio is None
    with pytest.raises(FileExistsError):
        prepare_performance(tmp_path, result.request, output_relative="prepared", tools=media_tools)
    assert (
        PreparedPerformance.model_validate_json((tmp_path / "prepared/manifest.json").read_text())
        == result
    )


def test_real_late_audio_start_retains_silence_instead_of_shifting_singing(
    tmp_path: Path, media_tools: MediaTools
) -> None:
    source = tmp_path / "delayed.mkv"
    _generate(media_tools, source, audio_delay=0.4)
    result = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path=source.name, duration_s=1.2),
        tools=media_tools,
    )
    assert result.source.audio_offset_s == pytest.approx(0.4, abs=0.002)
    assert result.audio.leading_silence_s == pytest.approx(0.4, abs=0.002)
    with wave.open(str(tmp_path / result.audio.path), "rb") as audio:
        silence = audio.readframes(int(0.35 * audio.getframerate()))
        sounding = audio.readframes(int(0.4 * audio.getframerate()))
    assert silence and set(silence) == {0}
    assert any(sounding)


def test_real_nonzero_pts_and_rotation_are_normalized_with_source_mapping(
    tmp_path: Path, media_tools: MediaTools
) -> None:
    plain = tmp_path / "plain.mkv"
    _generate(media_tools, plain)
    source = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            media_tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-display_rotation:v:0",
            "90",
            "-i",
            str(plain),
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-output_ts_offset",
            "3",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    result = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path=source.name, start_s=0.3, duration_s=1),
        tools=media_tools,
    )
    assert result.source.video.start_time_s == pytest.approx(3, abs=0.01)
    assert abs(result.source.video.rotation_degrees) == 90
    assert result.motion.width == 64 and result.motion.height == 96
    assert result.timeline.source_origin_pts_time_s == pytest.approx(3, abs=0.01)
    assert result.timeline.source_interval_start_pts_time_s == pytest.approx(3.3, abs=0.01)
    assert result.timeline.anchors[0].output_time_s == 0
    assert result.timeline.source_time(0.5) == pytest.approx(3.8, abs=0.01)


def test_symlink_outside_data_dir_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"must not read")
    link = root / "link.mp4"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable to this Windows account")
    monkeypatch.setattr(
        preparation, "_probe", lambda *args, **kwargs: pytest.fail("probed outside root")
    )
    with pytest.raises(ValueError, match="outside data_dir"):
        preparation.probe_source(root, link.name)
