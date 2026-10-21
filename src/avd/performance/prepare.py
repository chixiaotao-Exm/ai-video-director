"""Preserve source choreography timing while preparing silent video and PCM audio."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import uuid
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from avd.state.paths import relative_path

from .models import (
    AudioSource,
    FrameAnchor,
    PreparationRequest,
    PreparedAudio,
    PreparedPerformance,
    PreparedVideo,
    SourceManifest,
    StreamTiming,
    Timeline,
    VideoSource,
)


class PerformanceMediaError(RuntimeError):
    """A reference cannot be prepared without losing its timing contract."""


@dataclass(frozen=True)
class MediaTools:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    timeout_s: float = 600

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("Media tool timeout must be finite and positive")


def _run(arguments: list[str], tools: MediaTools) -> str:
    try:
        result = subprocess.run(
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
        raise PerformanceMediaError(f"Media tool failed: {detail}") from exc
    except OSError as exc:
        raise PerformanceMediaError(f"Cannot run media tool: {exc}") from exc
    return result.stdout


def _under_root(root: Path, value: str) -> Path:
    candidate = (root / relative_path(value)).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Media path resolves outside data_dir")
    return candidate


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _probe(path: Path, tools: MediaTools, *, frame_stream: int | None = None) -> dict[str, Any]:
    arguments = [tools.ffprobe, "-v", "error"]
    if frame_stream is not None:
        arguments += [
            "-select_streams",
            str(frame_stream),
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time,pts_time,pkt_duration_time,duration_time,nb_samples",
        ]
    else:
        arguments += ["-show_streams", "-show_format"]
    raw = _run([*arguments, "-of", "json", str(path)], tools)
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data
    except (ValueError, TypeError) as exc:
        raise PerformanceMediaError("ffprobe returned invalid JSON metadata") from exc


def _number(value: Any, default: float | None = None) -> float | None:
    if value in (None, "N/A"):
        return default
    if isinstance(value, bool):
        raise ValueError("timestamps must be finite numbers")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("timestamps must be finite numbers")
    return result


def _ratio(value: Any) -> str | None:
    if value in (None, "0/0", "N/A"):
        return None
    fraction = Fraction(str(value))
    if fraction <= 0:
        raise ValueError("time bases and frame rates must be positive")
    return str(fraction) if fraction.denominator != 1 else f"{fraction.numerator}/1"


def _timing(stream: dict[str, Any]) -> dict[str, Any]:
    time_base = _ratio(stream.get("time_base"))
    if time_base is None:
        raise ValueError("stream time_base is missing")
    start_pts = stream.get("start_pts")
    start = _number(stream.get("start_time"))
    if start is None:
        start = float(int(start_pts or 0) * Fraction(time_base))
    timing = StreamTiming(
        index=stream["index"],
        codec=stream.get("codec_name"),
        time_base=time_base,
        start_pts=int(start_pts) if start_pts is not None else None,
        start_time_s=start,
        duration_s=_number(stream.get("duration")),
    )
    return timing.model_dump()


def _video(stream: dict[str, Any]) -> VideoSource:
    rotation = next(
        (
            item["rotation"]
            for item in stream.get("side_data_list", [])
            if item.get("rotation") is not None
        ),
        stream.get("tags", {}).get("rotate", 0),
    )
    frames = stream.get("nb_frames")
    return VideoSource(
        **_timing(stream),
        width=stream["width"],
        height=stream["height"],
        rotation_degrees=float(rotation),
        avg_frame_rate=_ratio(stream.get("avg_frame_rate")),
        nominal_frame_rate=_ratio(stream.get("r_frame_rate")),
        reported_frame_count=int(frames) if frames not in (None, "N/A") else None,
    )


def _audio(stream: dict[str, Any]) -> AudioSource:
    return AudioSource(
        **_timing(stream), sample_rate=int(stream["sample_rate"]), channels=stream["channels"]
    )


def probe_source(
    data_dir: Path, source_relative: str, *, tools: MediaTools = MediaTools()
) -> SourceManifest:
    """Inspect a source without creating files or changing the source's timestamps."""
    root = Path(data_dir).expanduser().resolve()
    source = _under_root(root, source_relative)
    if not source.is_file() or source.stat().st_size == 0:
        raise FileNotFoundError("Source video must be a nonempty file under data_dir")
    data = _probe(source, tools)
    try:
        streams = data["streams"]
        video_stream = next(
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        )
        video = _video(video_stream)
        audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
        audio = _audio(audio_stream) if audio_stream else None
        container = data.get("format", {})
        duration = _number(container.get("duration"), video.duration_s)
        if duration is None:
            raise ValueError("source duration is missing")
        return SourceManifest(
            path=relative_path(source_relative),
            sha256=_hash(source),
            size_bytes=source.stat().st_size,
            container_start_time_s=_number(container.get("start_time"), 0) or 0,
            container_duration_s=duration,
            video=video,
            audio=audio,
            audio_offset_s=audio.start_time_s - video.start_time_s if audio else None,
        )
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        OverflowError,
        StopIteration,
        ZeroDivisionError,
    ) as exc:
        raise PerformanceMediaError(f"Source video metadata is incomplete: {exc}") from exc


def _frame_times(path: Path, tools: MediaTools, stream_index: int) -> list[float]:
    try:
        frames = _probe(path, tools, frame_stream=stream_index)["frames"]
        result = [
            _number(item.get("best_effort_timestamp_time", item.get("pts_time"))) for item in frames
        ]
        if not result or any(value is None for value in result):
            raise ValueError("every video frame needs a presentation timestamp")
        times = [float(value) for value in result if value is not None]
        if any(second <= first for first, second in zip(times, times[1:])):
            raise ValueError("source frame timestamps must be strictly increasing")
        return times
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PerformanceMediaError(f"Video frame timing cannot be mapped: {exc}") from exc


def _num(value: float) -> str:
    # FFmpeg duration options reject scientific notation for small offsets.
    return format(value, ".12f").rstrip("0").rstrip(".") or "0"


def _validate_audio_clock(
    source: Path, audio: AudioSource, start: float, end: float, tools: MediaTools
) -> None:
    """Reject timestamp gaps instead of silently concatenating displaced samples."""
    previous_end: float | None = None
    tolerance = max(2 * float(Fraction(audio.time_base)), 2 / audio.sample_rate, 2e-6)
    try:
        frames = _probe(source, tools, frame_stream=audio.index)["frames"]
        has_overlap = False
        for frame in frames:
            timestamp = _number(frame.get("best_effort_timestamp_time", frame.get("pts_time")))
            samples = int(frame["nb_samples"])
            if timestamp is None or samples <= 0:
                raise ValueError("every audio frame needs a timestamp and sample count")
            frame_end = timestamp + samples / audio.sample_rate
            if frame_end > start and timestamp < end:
                has_overlap = True
            if (
                previous_end is not None
                and timestamp < end
                and timestamp >= start
                and abs(timestamp - previous_end) > tolerance
            ):
                raise ValueError(
                    "audio has timestamp gaps or overlaps; repair the source audio first"
                )
            previous_end = frame_end
        if not has_overlap:
            raise ValueError("selected interval contains no audio samples")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PerformanceMediaError(f"Source audio timing cannot be preserved: {exc}") from exc


def _base(tools: MediaTools, source: Path) -> list[str]:
    return [
        tools.ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-copyts",
        "-i",
        str(source),
    ]


def _output_info(path: Path, root: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size == 0:
        raise PerformanceMediaError(f"Media tool produced no output: {path.name}")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _hash(path),
        "size_bytes": path.stat().st_size,
    }


def prepare_performance(
    data_dir: Path,
    request: PreparationRequest,
    *,
    output_relative: str | None = None,
    tools: MediaTools = MediaTools(),
) -> PreparedPerformance:
    """Decode an interval once, retaining VFR timestamps and source A/V offset.

    The interval starts at its first available source frame (at or after the
    requested start). Actual frame times and duration are measured from output;
    no FPS conversion, speed change, looping or duration stretching is applied.
    Existing output directories are never reused or overwritten.
    """
    root = Path(data_dir).expanduser().resolve()
    source = _under_root(root, request.source_video_path)
    folder = _under_root(root, output_relative or f"assets/performance/{uuid.uuid4().hex}")
    if folder.exists() or folder.is_symlink():
        raise FileExistsError("Refusing to overwrite an existing performance output directory")
    manifest = probe_source(root, request.source_video_path, tools=tools)
    if manifest.audio is None:
        raise PerformanceMediaError("Reference video has no audio track; upload a singing video")
    times = _frame_times(source, tools, manifest.video.index)
    origin = times[0]
    requested_start = origin + request.start_s
    requested_end = requested_start + request.duration_s
    # ffprobe reports seconds to microsecond precision. This tolerance prevents
    # a nominal 1/30 frame boundary being lost only through decimal rounding.
    selected = [time for time in times if requested_start - 5e-7 <= time < requested_end - 5e-7]
    if not selected:
        raise PerformanceMediaError("Requested interval contains no source video frames")
    anchor = selected[0]
    audio_end = (
        manifest.audio.start_time_s + manifest.audio.duration_s
        if manifest.audio.duration_s is not None
        else None
    )
    if manifest.audio.start_time_s >= requested_end or (
        audio_end is not None and audio_end <= anchor
    ):
        raise PerformanceMediaError("Requested interval has no overlapping source audio samples")
    _validate_audio_clock(source, manifest.audio, anchor, requested_end, tools)
    folder.mkdir(parents=True, exist_ok=False)
    motion_path, audio_path = folder / "motion.mp4", folder / "audio.wav"
    video_filter = (
        f"trim=start={_num(anchor - 5e-7)}:end={_num(requested_end - 5e-7)},"
        "setpts=PTS-STARTPTS,pad=ceil(iw/2)*2:ceil(ih/2)*2,setsar=1"
    )
    _run(
        [
            *_base(tools, source),
            "-map",
            f"0:{manifest.video.index}",
            "-an",
            "-vf",
            video_filter,
            "-fps_mode",
            "passthrough",
            "-enc_time_base",
            manifest.video.time_base,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            str(motion_path),
        ],
        tools,
    )
    prepared_video = probe_source(root, motion_path.relative_to(root).as_posix(), tools=tools)
    output_times = _frame_times(motion_path, tools, prepared_video.video.index)
    tolerance = max(float(Fraction(manifest.video.time_base)) * 2, 2e-6)
    if len(output_times) != len(selected) or any(
        abs(actual - (original - anchor)) > tolerance
        for actual, original in zip(output_times, selected)
    ):
        raise PerformanceMediaError("Prepared video changed source frame timing or frame count")
    duration = prepared_video.video.duration_s or prepared_video.container_duration_s
    if prepared_video.audio is not None:
        raise PerformanceMediaError("Prepared motion reference unexpectedly contains audio")
    # Subtract the same video anchor, not the audio stream's own start. Resampling
    # sets the sample format/rate; async=0 forbids clock correction/time stretching.
    # first_pts=0 pads a late audio start with silence while retaining its offset.
    audio_filter = (
        f"atrim=start={_num(anchor)}:end={_num(anchor + duration)},"
        f"asetpts=PTS-({_num(anchor)})/TB,"
        f"aresample={request.sample_rate}:async=0:first_pts=0,"
        f"apad,atrim=duration={_num(duration)}"
    )
    _run(
        [
            *_base(tools, source),
            "-map",
            f"0:{manifest.audio.index}",
            "-vn",
            "-af",
            audio_filter,
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(request.sample_rate),
            str(audio_path),
        ],
        tools,
    )
    try:
        with wave.open(str(audio_path), "rb") as stream:
            sample_rate, sample_count = stream.getframerate(), stream.getnframes()
            channels = stream.getnchannels()
    except (OSError, wave.Error) as exc:
        raise PerformanceMediaError("Prepared audio is not readable PCM WAV") from exc
    if (
        sample_rate != request.sample_rate
        or abs(sample_count / sample_rate - duration) > 1 / sample_rate
    ):
        raise PerformanceMediaError("Prepared audio duration does not match the video timeline")
    if _hash(source) != manifest.sha256:
        raise PerformanceMediaError(
            "Reference video changed during preparation; retry with a stable file"
        )
    try:
        result = PreparedPerformance(
            source=manifest,
            request=request,
            timeline=Timeline(
                requested_start_s=request.start_s,
                requested_duration_s=request.duration_s,
                source_origin_pts_time_s=origin,
                source_interval_start_pts_time_s=anchor,
                source_interval_end_pts_time_s=anchor + duration,
                actual_duration_s=duration,
                frame_count=len(output_times),
                frame_time_tolerance_s=tolerance,
                anchors=[
                    FrameAnchor(
                        frame_index=index,
                        output_time_s=output_time,
                        source_pts_time_s=source_time,
                        source_video_time_s=max(0, source_time - origin),
                    )
                    for index, (output_time, source_time) in enumerate(zip(output_times, selected))
                ],
            ),
            motion=PreparedVideo(
                **_output_info(motion_path, root),
                width=prepared_video.video.width,
                height=prepared_video.video.height,
                frame_count=len(output_times),
                time_base=prepared_video.video.time_base,
                avg_frame_rate=prepared_video.video.avg_frame_rate,
            ),
            audio=PreparedAudio(
                **_output_info(audio_path, root),
                sample_rate=sample_rate,
                channels=channels,
                sample_count=sample_count,
                duration_s=sample_count / sample_rate,
                leading_silence_s=max(0, min(duration, manifest.audio.start_time_s - anchor)),
            ),
            ffmpeg_version=_run([tools.ffmpeg, "-version"], tools).splitlines()[0],
            ffprobe_version=_run([tools.ffprobe, "-version"], tools).splitlines()[0],
        )
    except (ValidationError, IndexError) as exc:
        raise PerformanceMediaError(f"Prepared media manifest is invalid: {exc}") from exc
    with (folder / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(result.model_dump_json(indent=2) + "\n")
    return result
