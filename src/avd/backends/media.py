"""Deterministic, local FFmpeg steps with immutable output paths."""

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


class MediaProcessingError(RuntimeError):
    """A media tool is unavailable, failed, or returned unusable data."""


@dataclass(frozen=True)
class _VideoInfo:
    duration: float
    width: int
    height: int
    has_audio: bool


def _input_file(path: Path | str) -> Path:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Media input is not a file: {source}")
    if source.stat().st_size == 0:
        raise ValueError(f"Media input is empty: {source}")
    return source.resolve()


def _prepare_output(dest: Path, inputs: list[Path] | None = None) -> Path:
    output = Path(dest)
    resolved = output.resolve()
    if inputs and resolved in inputs:
        raise ValueError("Media output must differ from every input")
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Refusing to overwrite media output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _run(arguments: list[str], *, cwd: Path | None = None, timeout: int = 600) -> str:
    try:
        result = subprocess.run(
            arguments,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise MediaProcessingError(f"{arguments[0]} is required on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProcessingError(f"{arguments[0]} timed out after {timeout} seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise MediaProcessingError(f"{arguments[0]} failed: {detail}") from exc
    except OSError as exc:
        raise MediaProcessingError(f"Cannot run {arguments[0]}: {exc}") from exc
    return result.stdout


def _probe(source: Path) -> dict[str, Any]:
    raw = _run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)],
        timeout=120,
    )
    try:
        metadata = json.loads(raw)
        if not isinstance(metadata, dict):
            raise ValueError("expected an object")
        return metadata
    except (ValueError, TypeError) as exc:
        raise MediaProcessingError(f"Invalid ffprobe metadata for {source}: {exc}") from exc


def _positive_duration(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("duration must be finite and positive")
    duration = float(value)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be finite and positive")
    return duration


def _duration(metadata: dict[str, Any], source: Path, stream: dict[str, Any] | None = None) -> float:
    try:
        container = metadata.get("format", {})
        streams = metadata.get("streams", [])
        candidates = ([stream.get("duration")] if stream is not None else [])
        candidates.append(container.get("duration"))
        if stream is None:
            candidates.extend(item.get("duration") for item in streams)
        for candidate in candidates:
            if candidate is not None and candidate != "N/A":
                return _positive_duration(candidate)
        raise ValueError("no duration was reported")
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise MediaProcessingError(f"Invalid media duration for {source}: {exc}") from exc


def media_duration(path: Path | str) -> float:
    """Return a positive, finite container duration in seconds (audio or video)."""
    source = _input_file(path)
    return _duration(_probe(source), source)


def _video_info(source: Path) -> _VideoInfo:
    metadata = _probe(source)
    try:
        streams = metadata["streams"]
        video = next(item for item in streams if item.get("codec_type") == "video")
        width, height = video["width"], video["height"]
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (width, height)):
            raise ValueError("video dimensions must be positive integers")
        return _VideoInfo(
            duration=_duration(metadata, source, video),
            width=width,
            height=height,
            has_audio=any(item.get("codec_type") == "audio" for item in streams),
        )
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration) as exc:
        raise MediaProcessingError(f"Invalid video metadata for {source}: {exc}") from exc


def _timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_srt(lines: list[tuple[str, float]], dest: Path) -> Path:
    """Write sequential cues from text and voice duration pairs, exclusively.

    Cumulative times are rounded to milliseconds. Positive sub-millisecond
    durations receive a minimum one-millisecond cue so every cue has an end.
    """
    if not lines:
        raise ValueError("At least one subtitle line is required")
    cues: list[str] = []
    elapsed = Decimal(0)
    start_ms = 0
    for number, (text, raw_duration) in enumerate(lines, start=1):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Subtitle line {number} must contain text")
        try:
            duration = _positive_duration(raw_duration)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Subtitle line {number} duration must be finite and positive") from exc
        elapsed += Decimal(str(duration))
        end_ms = max(start_ms + 1, int((elapsed * 1000).to_integral_value(rounding=ROUND_HALF_UP)))
        # Blank lines end SRT cues; normalize whitespace without inventing cues.
        content = "\n".join(part.strip() for part in text.splitlines() if part.strip())
        cues.append(f"{number}\n{_timestamp(start_ms)} --> {_timestamp(end_ms)}\n{content}\n\n")
        start_ms = end_ms
    output = _prepare_output(dest)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("".join(cues))
    return Path(dest)


def _check_output(output: Path) -> None:
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaProcessingError(f"ffmpeg did not produce media at {output}")


def burn_subtitles(video: Path, srt: Path, dest: Path) -> Path:
    """Burn an SRT into a new H.264 video, preserving optional source audio."""
    source = _input_file(video)
    subtitles = _input_file(srt)
    output = _prepare_output(dest, [source, subtitles])
    # Filter expressions have their own escaping rules, including Windows drive
    # colons. A private, fixed basename avoids interpreting the user's path.
    with TemporaryDirectory(prefix=".avd-subtitles-", dir=output.parent) as temporary:
        working = Path(temporary)
        shutil.copyfile(subtitles, working / "subtitles.srt")
        _run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "subtitles=filename=subtitles.srt,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-movflags", "+faststart", str(output),
            ],
            cwd=working,
        )
    _check_output(output)
    return Path(dest)


def _clip_filters(index: int, info: _VideoInfo, width: int, height: int) -> list[str]:
    duration = format(info.duration, ".12g")
    video = (
        f"[{index}:v:0]setpts=PTS-STARTPTS,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"fps=30,format=yuv420p,settb=AVTB[v{index}]"
    )
    audio = (
        f"[{index}:a:0]asetpts=PTS-STARTPTS,aresample=48000,"
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad"
        if info.has_audio
        else "anullsrc=r=48000:cl=stereo"
    )
    return [video, f"{audio},atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"]


def assemble_final(clips: list[Path], dest: Path) -> Path:
    """Concatenate ordered shots, normalize audio, and visibly disclose AI use.

    Output uses the first clip's dimensions (rounded up to even), 30 fps,
    stereo 48 kHz audio, and silence for clips with no audio stream.
    """
    if not clips:
        raise ValueError("At least one video clip is required")
    sources = [_input_file(clip) for clip in clips]
    output = _prepare_output(dest, sources)
    information = [_video_info(source) for source in sources]
    width = information[0].width + information[0].width % 2
    height = information[0].height + information[0].height % 2
    filters: list[str] = []
    for index, info in enumerate(information):
        filters.extend(_clip_filters(index, info, width, height))
    ordered = "".join(f"[v{index}][a{index}]" for index in range(len(sources)))
    filters.extend([
        f"{ordered}concat=n={len(sources)}:v=1:a=1[video][audio]",
        "[video]drawtext=text='AI-generated':fontcolor=white:fontsize=h/24:"
        "box=1:boxcolor=black@0.6:boxborderw=4:x=w-tw-12:y=h-th-12[vout]",
        "[audio]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]",
    ])
    arguments = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
    for source in sources:
        arguments.extend(["-i", str(source)])
    arguments.extend([
        "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-metadata", "comment=AI-generated", "-movflags", "+faststart", str(output),
    ])
    _run(arguments, timeout=3600)
    _check_output(output)
    return Path(dest)
