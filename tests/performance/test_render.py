"""Real media proves that export preserves timing and refuses incomplete results."""

from __future__ import annotations

import array
import hashlib
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Callable

import pytest

from avd.performance import (
    MediaTools,
    PerformanceMediaError,
    PreparationRequest,
    prepare_performance,
)
from avd.performance.models import PreparedPerformance
from avd.performance.render import (
    MixedAudio,
    check_performance_sync,
    mix_performance_audio,
    render_performance,
)


@pytest.fixture
def media_tools() -> MediaTools:
    bundle = Path(__file__).resolve().parents[2] / "data/tools/ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
        pytest.skip("FFmpeg and ffprobe required for real synchronization checks")
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def _wav(
    path: Path,
    rate: int,
    duration: float,
    sample: Callable[[float], float],
    *,
    channels: int = 1,
) -> None:
    samples = array.array("h")
    for index in range(round(rate * duration)):
        value = round(max(-1, min(1, sample(index / rate))) * 32767)
        samples.extend([value] * channels)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


def _run(tools: MediaTools, arguments: list[str]) -> None:
    subprocess.run(
        [tools.ffmpeg, "-v", "error", "-nostdin", "-n", *arguments],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture
def performance_sample(tmp_path: Path, media_tools: MediaTools) -> PreparedPerformance:
    _wav(
        tmp_path / "original.wav",
        48000,
        1.2,
        lambda t: 0.03 * math.sin(2 * math.pi * 110 * t),
        channels=2,
    )
    _run(
        media_tools,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=96x64:rate=10:duration=1.2",
            "-i",
            str(tmp_path / "original.wav"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            str(tmp_path / "reference.mkv"),
        ],
    )
    prepared = prepare_performance(
        tmp_path,
        PreparationRequest(source_video_path="reference.mkv", duration_s=1.2),
        output_relative="prepared",
        tools=media_tools,
    )

    def vocal_sample(time: float) -> float:
        return (
            0.65 * math.sin(2 * math.pi * 440 * time)
            if any(start <= time < start + 0.035 for start in (0.1, 0.6, 1.1))
            else 0
        )

    _wav(tmp_path / "vocals.wav", 22050, 1.2, vocal_sample)
    _wav(
        tmp_path / "music.wav",
        44100,
        1.2,
        lambda t: 0.03 * math.sin(2 * math.pi * 110 * t),
        channels=2,
    )
    return prepared


def test_real_mix_resamples_and_limits_peaks_without_moving_or_overwriting_inputs(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    before = {
        name: hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        for name in ("vocals.wav", "music.wav")
    }
    result = mix_performance_audio(
        tmp_path,
        performance_sample,
        "vocals.wav",
        "music.wav",
        output_relative="mix",
        vocal_gain_db=12,
        accompaniment_gain_db=12,
        peak_limit_db=-1,
        tools=media_tools,
    )
    assert result.audio.sample_rate == 48000 and result.audio.channels == 2
    assert result.audio.sample_count == 57600
    assert result.audio.duration_s == 1.2
    assert result.attenuation_db < 0
    assert result.measurements.peak_db is not None and result.measurements.peak_db <= -1
    assert result.vocals.sample_rate == 22050 and result.accompaniment.sample_rate == 44100
    assert result == MixedAudio.model_validate_json(
        (tmp_path / "mix/mix_manifest.json").read_text()
    )
    for name, digest in before.items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    with pytest.raises(FileExistsError):
        mix_performance_audio(
            tmp_path,
            performance_sample,
            "vocals.wav",
            "music.wav",
            output_relative="mix",
            tools=media_tools,
        )


def _decoded_samples(tools: MediaTools, path: Path, dest: Path) -> tuple[int, array.array[int]]:
    _run(tools, ["-i", str(path), "-map", "0:a:0", "-ac", "1", "-c:a", "pcm_s16le", str(dest)])
    with wave.open(str(dest), "rb") as stream:
        rate = stream.getframerate()
        samples = array.array("h")
        samples.frombytes(stream.readframes(stream.getnframes()))
    return rate, samples


def test_real_mp4_keeps_beginning_middle_end_cues_and_all_frames(
    tmp_path: Path,
    media_tools: MediaTools,
    performance_sample: PreparedPerformance,
) -> None:
    mix = mix_performance_audio(
        tmp_path, performance_sample, "vocals.wav", "music.wav", tools=media_tools
    )
    sync = check_performance_sync(
        tmp_path,
        performance_sample,
        performance_sample.motion.path,
        mix.audio.path,
        tools=media_tools,
    )
    assert sync.actual_frame_count == 12
    assert [round(item.output_time_s, 1) for item in sync.anchors] == [0, 0.6, 1.1]
    assert all(anchor.video_error_s < 1e-5 for anchor in sync.anchors)
    final = render_performance(
        tmp_path,
        performance_sample,
        performance_sample.motion.path,
        mix.audio.path,
        output_relative="export",
        tools=media_tools,
    )
    assert final.output_frame_count == 12
    assert final.output_audio_duration_s == pytest.approx(1.2, abs=1 / 48000)
    assert final.output_video_duration_s == pytest.approx(1.2, abs=1e-5)
    assert final.ai_label is True
    assert (tmp_path / "export/render_manifest.json").is_file()
    rate, mixed = _decoded_samples(
        media_tools, tmp_path / mix.audio.path, tmp_path / "mixed_pcm.wav"
    )
    exported_rate, exported = _decoded_samples(
        media_tools, tmp_path / final.video.path, tmp_path / "export_pcm.wav"
    )
    assert exported_rate == rate
    for cue in (0.1, 0.6, 1.1):
        start, end = round((cue - 0.03) * rate), round((cue + 0.03) * rate)
        mixed_onset = next(index for index in range(start, end) if abs(mixed[index]) > 0.25 * 32767)
        export_onset = next(
            index for index in range(start, end) if abs(exported[index]) > 0.25 * 32767
        )
        assert abs(mixed_onset - export_onset) / rate < 0.005
    with pytest.raises(FileExistsError):
        render_performance(
            tmp_path,
            performance_sample,
            performance_sample.motion.path,
            mix.audio.path,
            output_relative="export",
            tools=media_tools,
        )


def test_unlabelled_export_is_rejected_before_reading_media_or_creating_output(
    tmp_path: Path,
    performance_sample: PreparedPerformance,
) -> None:
    with pytest.raises(PerformanceMediaError, match="require an AI-generated label"):
        render_performance(
            tmp_path,
            performance_sample,
            "missing-video.mp4",
            "missing-audio.wav",
            output_relative="rejected",
            ai_label=False,
        )
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("duration", [1.1, 1.3])
def test_short_or_long_voice_is_rejected_before_any_output(
    tmp_path: Path,
    media_tools: MediaTools,
    performance_sample: PreparedPerformance,
    duration: float,
) -> None:
    _wav(tmp_path / "wrong.wav", 48000, duration, lambda t: 0.2 * math.sin(440 * t))
    with pytest.raises(PerformanceMediaError, match="Audio duration mismatch"):
        mix_performance_audio(
            tmp_path,
            performance_sample,
            "wrong.wav",
            "music.wav",
            output_relative="rejected",
            tools=media_tools,
        )
    assert not (tmp_path / "rejected").exists()


def test_silent_voice_or_final_mix_is_rejected(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    _wav(tmp_path / "silent.wav", 48000, 1.2, lambda t: 0)
    with pytest.raises(PerformanceMediaError, match="silent or inaudible"):
        mix_performance_audio(
            tmp_path, performance_sample, "silent.wav", "music.wav", tools=media_tools
        )
    with pytest.raises(PerformanceMediaError, match="silent or inaudible"):
        render_performance(
            tmp_path,
            performance_sample,
            performance_sample.motion.path,
            "silent.wav",
            output_relative="rejected",
            tools=media_tools,
        )
    assert not (tmp_path / "rejected").exists()


def test_lost_middle_frame_is_rejected_even_when_duration_still_matches(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    _run(
        media_tools,
        [
            "-i",
            str(tmp_path / performance_sample.motion.path),
            "-vf",
            "select=not(eq(n\\,5))",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            performance_sample.motion.time_base,
            "-c:v",
            "libx264",
            "-an",
            str(tmp_path / "dropped.mp4"),
        ],
    )
    with pytest.raises(PerformanceMediaError, match="frame count mismatch"):
        check_performance_sync(
            tmp_path,
            performance_sample,
            "dropped.mp4",
            performance_sample.audio.path,
            tools=media_tools,
        )


def test_model_native_frame_count_must_be_explicit_and_duration_remains_strict(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    _run(
        media_tools,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=96x64:rate=5:duration=1.2",
            "-c:v",
            "libx264",
            "-an",
            str(tmp_path / "native.mp4"),
        ],
    )
    with pytest.raises(PerformanceMediaError, match="frame count mismatch"):
        check_performance_sync(
            tmp_path,
            performance_sample,
            "native.mp4",
            performance_sample.audio.path,
            tools=media_tools,
        )
    report = check_performance_sync(
        tmp_path,
        performance_sample,
        "native.mp4",
        performance_sample.audio.path,
        expected_frame_count=6,
        tools=media_tools,
    )
    assert report.actual_frame_count == 6 and report.video_duration_s == 1.2
    _run(
        media_tools,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=96x64:rate=12:duration=1",
            "-c:v",
            "libx264",
            "-an",
            str(tmp_path / "too_fast.mp4"),
        ],
    )
    with pytest.raises(PerformanceMediaError, match="Video duration mismatch"):
        check_performance_sync(
            tmp_path,
            performance_sample,
            "too_fast.mp4",
            performance_sample.audio.path,
            tools=media_tools,
        )


def test_native_model_internal_timestamp_drift_is_rejected(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    _run(
        media_tools,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:size=96x64:rate=5:duration=1.2",
            "-vf",
            "settb=expr=1/1000,setpts=if(eq(N\\,2)\\,500\\,PTS)",
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            "1/1000",
            "-c:v",
            "libx264",
            "-an",
            str(tmp_path / "drifted.mp4"),
        ],
    )
    with pytest.raises(PerformanceMediaError, match="declared cadence"):
        check_performance_sync(
            tmp_path,
            performance_sample,
            "drifted.mp4",
            performance_sample.audio.path,
            expected_frame_count=6,
            tools=media_tools,
        )


def test_video_with_audio_stream_first_is_exported_with_the_selected_mix(
    tmp_path: Path, media_tools: MediaTools, performance_sample: PreparedPerformance
) -> None:
    _run(
        media_tools,
        [
            "-i",
            str(tmp_path / "original.wav"),
            "-i",
            str(tmp_path / performance_sample.motion.path),
            "-map",
            "0:a:0",
            "-map",
            "1:v:0",
            "-c:a",
            "aac",
            "-c:v",
            "copy",
            str(tmp_path / "audio_first.mp4"),
        ],
    )
    mix = mix_performance_audio(
        tmp_path, performance_sample, "vocals.wav", "music.wav", tools=media_tools
    )
    result = render_performance(
        tmp_path, performance_sample, "audio_first.mp4", mix.audio.path, tools=media_tools
    )
    assert result.sync.video_stream_index == 1
    assert result.output_frame_count == 12
    rate, exported = _decoded_samples(
        media_tools, tmp_path / result.video.path, tmp_path / "replacement_pcm.wav"
    )
    assert (
        max(abs(sample) for sample in exported[round(0.605 * rate) : round(0.625 * rate)])
        > 0.4 * 32767
    )
