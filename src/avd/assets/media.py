"""Reusable video/audio clips in the asset library, e.g. from splitting a video."""

from __future__ import annotations

import json
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Float, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.state.models import Base


class MediaError(RuntimeError):
    """FFmpeg is missing or rejected the source media."""


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # video | audio
    path: Mapped[str] = mapped_column(String, nullable=False)  # relative to data_dir
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def asset_data(asset: MediaAsset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "name": asset.name,
        "kind": asset.kind,
        "path": asset.path,
        "source": asset.source,
        "project_id": asset.project_id,
        "duration_s": asset.duration_s,
        "created_at": asset.created_at,
    }


def _run(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=1800)
    except FileNotFoundError as exc:
        raise MediaError(f"{arguments[0]} is required on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{arguments[0]} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join((exc.stderr or "").split())[-400:]
        raise MediaError(f"{arguments[0]} failed: {detail}") from exc
    return result.stdout


def _streams(source: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None, float | None]:
    raw = _run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(source)]
    )
    data = json.loads(raw)
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    duration = data.get("format", {}).get("duration")
    return video, audio, float(duration) if duration not in (None, "N/A") else None


def _encode(arguments: list[str], fallback: list[str], output: Path) -> None:
    """Try a lossless stream copy first; re-encode when the container rejects it."""
    try:
        _run(arguments)
    except MediaError:
        output.unlink(missing_ok=True)
        _run(fallback)
    if not output.is_file() or output.stat().st_size == 0:
        raise MediaError(f"FFmpeg produced no output at {output.name}")


def separate(
    session: Session,
    data_dir: Path,
    source_relative: str,
    *,
    name: str,
    project_id: str | None = None,
) -> list[MediaAsset]:
    """Split a video into a silent video track and an audio track, both saved as assets."""
    root = data_dir.expanduser().resolve()
    source = (root / source_relative).resolve()
    if not source.is_relative_to(root) or not source.is_file():
        raise FileNotFoundError("The source video must be an existing file under data_dir")
    video, audio, duration = _streams(source)
    if video is None:
        raise MediaError("The source has no video track")
    folder = root / "assets" / "media" / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
    created: list[MediaAsset] = []

    video_out = folder / "video.mp4"
    _encode(
        [*base, "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", str(video_out)],
        [
            *base,
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(video_out),
        ],
        video_out,
    )
    created.append(
        MediaAsset(
            name=f"{name} · 画面（无声）",
            kind="video",
            path=video_out.relative_to(root).as_posix(),
            source=source_relative,
            project_id=project_id,
            duration_s=duration,
        )
    )
    if audio is not None:
        # AAC copies losslessly into .m4a; anything else becomes 16-bit WAV.
        audio_out = folder / "audio.wav"
        if audio.get("codec_name") == "aac":
            try:
                _run([*base, "-map", "0:a:0", "-vn", "-c:a", "copy", str(folder / "audio.m4a")])
                audio_out = folder / "audio.m4a"
            except MediaError:
                (folder / "audio.m4a").unlink(missing_ok=True)
        if audio_out.suffix == ".wav":
            _run([*base, "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", str(audio_out)])
        if not audio_out.is_file() or audio_out.stat().st_size == 0:
            raise MediaError("FFmpeg produced no audio output")
        created.append(
            MediaAsset(
                name=f"{name} · 音频",
                kind="audio",
                path=audio_out.relative_to(root).as_posix(),
                source=source_relative,
                project_id=project_id,
                duration_s=duration,
            )
        )
    session.add_all(created)
    session.flush()
    return created


def list_assets(session: Session) -> list[MediaAsset]:
    return list(session.scalars(select(MediaAsset).order_by(MediaAsset.created_at.desc())))


__all__ = ["MediaAsset", "MediaError", "asset_data", "list_assets", "separate"]
