"""FFmpeg media operations. Every invocation uses an argument list."""

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from avd.agents.analysis.models import MediaInfo


class MediaProcessingError(RuntimeError):
    """A required media tool is missing or rejected its input."""


def _run(arguments: list[str], timeout: float) -> str:
    try:
        result = subprocess.run(
            arguments, capture_output=True, text=True, check=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise MediaProcessingError(f"{arguments[0]} is required on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaProcessingError(f"{arguments[0]} timed out after {timeout:g} seconds") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc)).strip()
        raise MediaProcessingError(f"{arguments[0]} failed: {detail}") from exc
    return result.stdout


def _duration(stream: dict[str, Any], metadata: dict[str, Any]) -> float:
    for candidate in (stream.get("duration"), metadata.get("format", {}).get("duration")):
        if candidate is not None and candidate != "N/A":
            return float(candidate)
    raise ValueError("ffprobe did not report a video duration")


def _fps(stream: dict[str, Any]) -> float:
    for name in ("avg_frame_rate", "r_frame_rate"):
        candidate = stream.get(name)
        if candidate and candidate not in ("0/0", "0", "N/A"):
            return float(Fraction(candidate))
    raise ValueError("ffprobe did not report a video frame rate")


def probe(video: Path | str) -> MediaInfo:
    """Read the first video stream and whether the container has any audio."""
    raw = _run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video)],
        timeout=120,
    )
    try:
        metadata = json.loads(raw)
        streams = metadata["streams"]
        video_stream = next(stream for stream in streams if stream.get("codec_type") == "video")
        return MediaInfo(
            duration_s=_duration(video_stream, metadata),
            width=video_stream["width"],
            height=video_stream["height"],
            fps=_fps(video_stream),
            has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
        )
    except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError, ValidationError) as exc:
        raise MediaProcessingError(f"Invalid or missing video metadata for {video}: {exc}") from exc


def extract_audio(video: Path | str, output_path: Path | str | None = None) -> Path:
    """Create mono 16 kHz PCM WAV suitable for speech recognition."""
    source = Path(video)
    output = Path(output_path) if output_path is not None else source.with_suffix(".analysis.wav")
    if source.resolve() == output.resolve():
        raise ValueError("Audio output must differ from the source video")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite audio output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            "-f", "wav", str(output),
        ],
        timeout=600,
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaProcessingError(f"ffmpeg did not produce audio at {output}")
    return output
