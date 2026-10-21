"""Measured mixing and export for a prepared performance timeline.

These deterministic steps do not generate a voice, identity or choreography.
Duration mismatches fail before mixing/export; inputs are never looped, stretched
or silently truncated to hide an incomplete model result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import PerformanceModel, PreparedFile, PreparedPerformance
from .prepare import (
    MediaTools,
    PerformanceMediaError,
    _frame_times,
    _hash,
    _num,
    _probe,
    _under_root,
)


class AudioMeasurements(PerformanceModel):
    peak_db: float | None
    rms_db: float | None


class AudioInput(PreparedFile):
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_count: int = Field(gt=0)
    duration_s: float = Field(gt=0)


class MixParameters(PerformanceModel):
    vocal_gain_db: float = Field(default=0, ge=-60, le=24)
    accompaniment_gain_db: float = Field(default=0, ge=-60, le=24)
    peak_limit_db: float = Field(default=-1, ge=-12, le=0)


class MixedAudio(PerformanceModel):
    stage: Literal["performance_mix"] = "performance_mix"
    source_sha256: str
    preparation_sha256: str
    vocals: AudioInput
    accompaniment: AudioInput
    parameters: MixParameters
    attenuation_db: float
    vocal_measurements: AudioMeasurements
    accompaniment_measurements: AudioMeasurements
    audio: AudioInput
    measurements: AudioMeasurements
    ffmpeg_version: str


class SyncAnchor(PerformanceModel):
    output_time_s: float
    source_pts_time_s: float
    video_frame_index: int = Field(ge=0)
    video_frame_time_s: float
    video_error_s: float = Field(ge=0)
    audio_sample_index: int = Field(ge=0)


class SyncReport(PerformanceModel):
    stage: Literal["performance_sync"] = "performance_sync"
    source_sha256: str
    preparation_sha256: str
    video: PreparedFile
    audio: AudioInput
    expected_frame_count: int = Field(gt=0)
    actual_frame_count: int = Field(gt=0)
    video_stream_index: int = Field(ge=0)
    video_time_base: str
    frame_timing_policy: Literal["source_frame_timestamps", "native_constant_frame_rate"]
    expected_duration_s: float = Field(gt=0)
    video_duration_s: float = Field(gt=0)
    duration_tolerance_s: float = Field(gt=0)
    measurements: AudioMeasurements
    anchors: list[SyncAnchor] = Field(min_length=3, max_length=3)


class RenderedPerformance(PerformanceModel):
    stage: Literal["performance_final"] = "performance_final"
    source_sha256: str
    preparation_sha256: str
    ai_label: bool
    video: PreparedFile
    sync: SyncReport
    encoded_audio_measurements: AudioMeasurements
    output_frame_count: int = Field(gt=0)
    output_video_duration_s: float = Field(gt=0)
    output_audio_duration_s: float = Field(gt=0)
    ffmpeg_version: str


def _capture(arguments: list[str], tools: MediaTools) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=tools.timeout_s,
        )
    except FileNotFoundError as exc:
        raise PerformanceMediaError(f"Required media tool is unavailable: {arguments[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PerformanceMediaError(f"Media tool timed out: {arguments[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join((exc.stderr or exc.stdout or "").split())[-800:]
        raise PerformanceMediaError(f"Performance export failed: {detail}") from exc
    except OSError as exc:
        raise PerformanceMediaError(f"Cannot run media tool: {exc}") from exc


def _file(root: Path, relative: str) -> PreparedFile:
    path = _under_root(root, relative)
    if not path.is_file() or path.stat().st_size == 0:
        raise PerformanceMediaError("Performance input must be a nonempty file under data_dir")
    return PreparedFile(
        path=path.relative_to(root).as_posix(), sha256=_hash(path), size_bytes=path.stat().st_size
    )


def _preparation_hash(prepared: PreparedPerformance) -> str:
    canonical = json.dumps(prepared.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _audio_input(root: Path, relative: str, tools: MediaTools) -> AudioInput:
    record = _file(root, relative)
    path = root / record.path
    if path.suffix.lower() != ".wav":
        raise PerformanceMediaError("Performance stems and mix must be WAV files")
    data = _probe(path, tools)
    try:
        stream = next(item for item in data["streams"] if item.get("codec_type") == "audio")
        sample_rate = int(stream["sample_rate"])
        # WAV exposes a sample-accurate duration_ts. Container duration strings
        # are rounded and cannot prove that the singing timeline is intact.
        sample_count_raw = (
            Fraction(str(stream["duration_ts"])) * Fraction(stream["time_base"]) * sample_rate
        )
        sample_count = round(sample_count_raw)
        if abs(sample_count_raw - sample_count) > Fraction(1, 1000000):
            raise ValueError("WAV sample count is not integral")
        if abs(float(stream.get("start_time", 0))) > 1 / sample_rate:
            raise ValueError("WAV must start at timeline zero")
        return AudioInput(
            **record.model_dump(),
            sample_rate=sample_rate,
            channels=int(stream["channels"]),
            sample_count=sample_count,
            duration_s=sample_count / sample_rate,
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        StopIteration,
    ) as exc:
        raise PerformanceMediaError(f"WAV timing cannot be measured: {exc}") from exc


def _measure_audio(
    path: Path, tools: MediaTools, *, sample_rate: int | None = None, channels: int | None = None
) -> AudioMeasurements:
    filters = []
    if sample_rate:
        filters.append(f"aresample={sample_rate}:async=0")
    if channels:
        filters.append(f"aformat=channel_layouts={channels}c")
    filters.append("astats=reset=0:measure_perchannel=none:measure_overall=Peak_level+RMS_level")
    result = _capture(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            ",".join(filters),
            "-f",
            "null",
            "-",
        ],
        tools,
    )

    def value(label: str) -> float | None:
        matches = re.findall(rf"{label}:\s*([^\s]+)", result.stderr)
        if not matches:
            raise PerformanceMediaError("FFmpeg did not report audio peak/RMS measurements")
        parsed = float(matches[-1])
        if parsed == -math.inf:
            return None
        if not math.isfinite(parsed):
            raise PerformanceMediaError("Audio contains nonfinite samples")
        return parsed

    return AudioMeasurements(peak_db=value("Peak level dB"), rms_db=value("RMS level dB"))


def _audible(measurements: AudioMeasurements, label: str) -> None:
    if measurements.rms_db is None or measurements.rms_db < -90:
        raise PerformanceMediaError(
            f"{label} is silent or inaudible; inspect the vocal/model output"
        )


def _audio_duration(audio: AudioInput, prepared: PreparedPerformance) -> None:
    tolerance = max(1 / audio.sample_rate, 1 / prepared.audio.sample_rate)
    if abs(audio.duration_s - prepared.timeline.actual_duration_s) > tolerance + 1e-9:
        raise PerformanceMediaError(
            f"Audio duration mismatch: expected {prepared.timeline.actual_duration_s:.6f}s, "
            f"received {audio.duration_s:.6f}s; no stretching or truncation was applied"
        )


def _new_folder(root: Path, relative: str | None, category: str) -> Path:
    folder = _under_root(root, relative or f"assets/{category}/{uuid.uuid4().hex}")
    if folder.exists() or folder.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing performance output directory")
    folder.mkdir(parents=True, exist_ok=False)
    return folder


def _write_manifest(folder: Path, name: str, value: PerformanceModel) -> None:
    with (folder / name).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value.model_dump_json(indent=2) + "\n")


def mix_performance_audio(
    data_dir: Path,
    prepared: PreparedPerformance,
    vocals_relative: str,
    accompaniment_relative: str,
    *,
    output_relative: str | None = None,
    vocal_gain_db: float = 0,
    accompaniment_gain_db: float = 0,
    peak_limit_db: float = -1,
    tools: MediaTools = MediaTools(),
) -> MixedAudio:
    """Mix aligned WAV stems with measured static headroom and no time correction."""
    parameters = MixParameters(
        vocal_gain_db=vocal_gain_db,
        accompaniment_gain_db=accompaniment_gain_db,
        peak_limit_db=peak_limit_db,
    )
    root = Path(data_dir).expanduser().resolve()
    vocals = _audio_input(root, vocals_relative, tools)
    accompaniment = _audio_input(root, accompaniment_relative, tools)
    for audio in (vocals, accompaniment):
        _audio_duration(audio, prepared)
    rate, channels = prepared.audio.sample_rate, prepared.audio.channels
    vocal_stats = _measure_audio(root / vocals.path, tools, sample_rate=rate, channels=channels)
    accompaniment_stats = _measure_audio(
        root / accompaniment.path, tools, sample_rate=rate, channels=channels
    )
    _audible(vocal_stats, "Converted singing voice")
    gains = (10 ** (vocal_gain_db / 20), 10 ** (accompaniment_gain_db / 20))
    peaks = [
        0 if item.peak_db is None else 10 ** (item.peak_db / 20)
        for item in (vocal_stats, accompaniment_stats)
    ]
    # The sum of the individual absolute peaks is a conservative bound. A tiny
    # quantization margin keeps 24-bit PCM rounding below the requested ceiling.
    maximum = sum(gain * peak for gain, peak in zip(gains, peaks))
    ceiling = 10 ** ((peak_limit_db - 0.005) / 20)
    attenuation = min(1.0, ceiling / maximum) if maximum else 1.0
    weights = " ".join(_num(gain * attenuation) for gain in gains)
    folder = _new_folder(root, output_relative, "performance-mixes")
    output = folder / "mixed.wav"
    filters = (
        f"[0:a:0]aresample={rate}:async=0,aformat=channel_layouts={channels}c[v];"
        f"[1:a:0]aresample={rate}:async=0,aformat=channel_layouts={channels}c[m];"
        f"[v][m]amix=inputs=2:normalize=0:duration=longest:dropout_transition=0:weights='{weights}'[a]"
    )
    _capture(
        [
            tools.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(root / vocals.path),
            "-i",
            str(root / accompaniment.path),
            "-filter_complex",
            filters,
            "-map",
            "[a]",
            "-c:a",
            "pcm_s24le",
            "-ar",
            str(rate),
            "-ac",
            str(channels),
            str(output),
        ],
        tools,
    )
    audio = _audio_input(root, output.relative_to(root).as_posix(), tools)
    _audio_duration(audio, prepared)
    measurements = _measure_audio(output, tools)
    _audible(measurements, "Mixed audio")
    if measurements.peak_db is None or measurements.peak_db > peak_limit_db + 0.001:
        raise PerformanceMediaError("Mixed audio exceeded its requested peak ceiling")
    if (
        _hash(root / vocals.path) != vocals.sha256
        or _hash(root / accompaniment.path) != accompaniment.sha256
    ):
        raise PerformanceMediaError("An input stem changed while mixing; retry with stable files")
    result = MixedAudio(
        source_sha256=prepared.source.sha256,
        preparation_sha256=_preparation_hash(prepared),
        vocals=vocals,
        accompaniment=accompaniment,
        parameters=parameters,
        attenuation_db=20 * math.log10(attenuation),
        vocal_measurements=vocal_stats,
        accompaniment_measurements=accompaniment_stats,
        audio=audio,
        measurements=measurements,
        ffmpeg_version=_capture([tools.ffmpeg, "-version"], tools).stdout.splitlines()[0],
    )
    _write_manifest(folder, "mix_manifest.json", result)
    return result


def check_performance_sync(
    data_dir: Path,
    prepared: PreparedPerformance,
    video_relative: str,
    audio_relative: str,
    *,
    expected_frame_count: int | None = None,
    native_frame_rate: str | None = None,
    tools: MediaTools = MediaTools(),
) -> SyncReport:
    """Prove duration/frame coverage and report start/middle/end timing anchors.

    A native model with another FPS must supply the frame count from its recipe.
    This does not relax the canonical duration requirement or prove motion quality.
    """
    root = Path(data_dir).expanduser().resolve()
    video = _file(root, video_relative)
    audio = _audio_input(root, audio_relative, tools)
    _audio_duration(audio, prepared)
    if expected_frame_count is not None and (
        not isinstance(expected_frame_count, int)
        or isinstance(expected_frame_count, bool)
        or expected_frame_count < 1
    ):
        raise ValueError("Expected video frame count must be a positive integer")
    expected = expected_frame_count or prepared.timeline.frame_count
    rate = None
    if native_frame_rate is not None:
        try:
            rate = Fraction(native_frame_rate)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError("Native frame rate must be a positive rational number") from exc
        if rate <= 0 or expected_frame_count is None:
            raise ValueError("Native frame rate requires an explicit positive frame count")
    raw = _probe(root / video.path, tools)
    try:
        stream = next(item for item in raw["streams"] if item.get("codec_type") == "video")
        time_base = float(Fraction(stream["time_base"]))
        duration = float(Fraction(str(stream["duration_ts"])) * Fraction(stream["time_base"]))
        times = _frame_times(root / video.path, tools, int(stream["index"]))
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        StopIteration,
    ) as exc:
        raise PerformanceMediaError(f"Generated video timing cannot be measured: {exc}") from exc
    tolerance = max(prepared.timeline.frame_time_tolerance_s, time_base * 2, 1e-5)
    if len(times) != expected:
        raise PerformanceMediaError(
            f"Video frame count mismatch: expected {expected}, received {len(times)}"
        )
    if abs(times[0]) > tolerance:
        raise PerformanceMediaError("Generated video must begin at timeline zero")
    if abs(duration - prepared.timeline.actual_duration_s) > tolerance:
        raise PerformanceMediaError(
            f"Video duration mismatch: expected {prepared.timeline.actual_duration_s:.6f}s, "
            f"received {duration:.6f}s; incomplete generation cannot be exported"
        )
    native_cadence = rate is not None or expected != prepared.timeline.frame_count
    if rate is not None and abs(float(expected / rate) - duration) > tolerance:
        raise PerformanceMediaError("Native model duration differs from its declared frame rate")
    if not native_cadence and any(
        abs(time - anchor.output_time_s) > tolerance
        for time, anchor in zip(times, prepared.timeline.anchors)
    ):
        raise PerformanceMediaError(
            "Video frame timestamps drifted from the prepared choreography timeline"
        )
    if native_cadence and any(
        abs(time - index * duration / expected) > tolerance for index, time in enumerate(times)
    ):
        raise PerformanceMediaError(
            "Native model frame timestamps drifted from its declared cadence"
        )
    stats = _measure_audio(root / audio.path, tools)
    _audible(stats, "Final singing mix")
    anchors = []
    original = prepared.timeline.anchors
    for item in (original[0], original[len(original) // 2], original[-1]):
        index = min(range(len(times)), key=lambda idx: abs(times[idx] - item.output_time_s))
        anchors.append(
            SyncAnchor(
                output_time_s=item.output_time_s,
                source_pts_time_s=item.source_pts_time_s,
                video_frame_index=index,
                video_frame_time_s=times[index],
                video_error_s=abs(times[index] - item.output_time_s),
                audio_sample_index=round(item.output_time_s * audio.sample_rate),
            )
        )
    return SyncReport(
        source_sha256=prepared.source.sha256,
        preparation_sha256=_preparation_hash(prepared),
        video=video,
        audio=audio,
        expected_frame_count=expected,
        actual_frame_count=len(times),
        video_stream_index=int(stream["index"]),
        video_time_base=stream["time_base"],
        frame_timing_policy="native_constant_frame_rate"
        if native_cadence
        else "source_frame_timestamps",
        expected_duration_s=prepared.timeline.actual_duration_s,
        video_duration_s=duration,
        duration_tolerance_s=tolerance,
        measurements=stats,
        anchors=anchors,
    )


def render_performance(
    data_dir: Path,
    prepared: PreparedPerformance,
    video_relative: str,
    audio_relative: str,
    *,
    output_relative: str | None = None,
    expected_frame_count: int | None = None,
    native_frame_rate: str | None = None,
    ai_label: bool = True,
    tools: MediaTools = MediaTools(),
) -> RenderedPerformance:
    """Export a measured performance with mandatory visible and metadata AI labels."""
    if ai_label is not True:
        raise PerformanceMediaError("Performance exports require an AI-generated label")
    root = Path(data_dir).expanduser().resolve()
    sync = check_performance_sync(
        root,
        prepared,
        video_relative,
        audio_relative,
        expected_frame_count=expected_frame_count,
        native_frame_rate=native_frame_rate,
        tools=tools,
    )
    folder = _new_folder(root, output_relative, "performance-exports")
    output = folder / "final.mp4"
    arguments = [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        str(root / sync.video.path),
        "-i",
        str(root / sync.audio.path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
    ]
    arguments += [
        "-vf",
        "drawtext=text='AI-generated':fontcolor=white:fontsize=h/24:"
        "box=1:boxcolor=black@0.6:boxborderw=4:x=w-tw-12:y=h-th-12",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-enc_time_base",
        sync.video_time_base,
        "-metadata",
        "comment=AI-generated",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        str(sync.audio.sample_rate),
        "-ac",
        str(sync.audio.channels),
        "-movflags",
        "+faststart",
        str(output),
    ]
    _capture(arguments, tools)
    output_record = _file(root, output.relative_to(root).as_posix())
    raw = _probe(output, tools)
    try:
        video = next(item for item in raw["streams"] if item.get("codec_type") == "video")
        audio = next(item for item in raw["streams"] if item.get("codec_type") == "audio")
        video_duration = float(Fraction(str(video["duration_ts"])) * Fraction(video["time_base"]))
        audio_duration = float(Fraction(str(audio["duration_ts"])) * Fraction(audio["time_base"]))
        output_times = _frame_times(output, tools, int(video["index"]))
        input_times = _frame_times(root / sync.video.path, tools, sync.video_stream_index)
        if len(output_times) != sync.expected_frame_count or any(
            abs(actual - expected) > sync.duration_tolerance_s
            for actual, expected in zip(output_times, input_times)
        ):
            raise ValueError("export dropped frames or changed their timestamps")
        if abs(video_duration - sync.expected_duration_s) > sync.duration_tolerance_s:
            raise ValueError("export changed the video duration")
        if abs(audio_duration - sync.expected_duration_s) > 1 / sync.audio.sample_rate + 1e-9:
            raise ValueError("export changed the singing duration")
        if abs(float(audio.get("start_time", 0))) > 1 / sync.audio.sample_rate:
            raise ValueError("export shifted the audio start")
        if raw.get("format", {}).get("tags", {}).get("comment") != "AI-generated":
            raise ValueError("AI label metadata is missing")
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        StopIteration,
    ) as exc:
        raise PerformanceMediaError(f"Final MP4 failed timeline verification: {exc}") from exc
    measurements = _measure_audio(output, tools)
    _audible(measurements, "Exported audio")
    if measurements.peak_db is not None and measurements.peak_db > 0.001:
        raise PerformanceMediaError(
            "Encoded audio clips; lower the mix peak limit and export again"
        )
    if (
        _hash(root / sync.video.path) != sync.video.sha256
        or _hash(root / sync.audio.path) != sync.audio.sha256
    ):
        raise PerformanceMediaError("Performance input changed while exporting")
    result = RenderedPerformance(
        source_sha256=prepared.source.sha256,
        preparation_sha256=_preparation_hash(prepared),
        ai_label=ai_label,
        video=output_record,
        sync=sync,
        encoded_audio_measurements=measurements,
        output_frame_count=len(output_times),
        output_video_duration_s=video_duration,
        output_audio_duration_s=audio_duration,
        ffmpeg_version=_capture([tools.ffmpeg, "-version"], tools).stdout.splitlines()[0],
    )
    _write_manifest(folder, "render_manifest.json", result)
    return result
