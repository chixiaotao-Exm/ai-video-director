from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from avd.assets.media import MediaAsset, MediaError, list_assets, separate
from avd.state.db import create_engine, create_session_factory, init_db

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg required"
)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def _clip(path: Path, *, audio: bool) -> None:
    arguments = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=160x90:d=1"]
    if audio:
        arguments += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac"]
    arguments += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", str(path)]
    subprocess.run(arguments, check=True, timeout=60)


def _streams(path: Path) -> set[str]:
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return {stream["codec_type"] for stream in json.loads(raw)["streams"]}


def test_separate_saves_silent_video_and_audio(tmp_path: Path, session: Session) -> None:
    (tmp_path / "uploads").mkdir()
    _clip(tmp_path / "uploads" / "song.mp4", audio=True)
    created = separate(session, tmp_path, "uploads/song.mp4", name="弹唱")
    assert [asset.kind for asset in created] == ["video", "audio"]
    video, audio = (tmp_path / asset.path for asset in created)
    assert _streams(video) == {"video"}
    assert _streams(audio) == {"audio"}
    assert audio.suffix == ".m4a"
    assert all(not Path(asset.path).is_absolute() for asset in created)
    assert {asset.id for asset in list_assets(session)} == {asset.id for asset in created}


def test_video_without_audio_gives_only_video(tmp_path: Path, session: Session) -> None:
    _clip(tmp_path / "silent.mp4", audio=False)
    created = separate(session, tmp_path, "silent.mp4", name="无声")
    assert [asset.kind for asset in created] == ["video"]


def test_rejects_paths_outside_data_dir(tmp_path: Path, session: Session) -> None:
    with pytest.raises(FileNotFoundError):
        separate(session, tmp_path, "../outside.mp4", name="x")
    (tmp_path / "not-video.mp4").write_bytes(b"not a video")
    with pytest.raises(MediaError):
        separate(session, tmp_path, "not-video.mp4", name="x")
    assert session.query(MediaAsset).count() == 0


def test_stems_require_demucs(
    tmp_path: Path, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from avd.assets import media

    monkeypatch.setattr(media.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(MediaError, match="Demucs"):
        media.split_vocals(tmp_path / "audio.wav", tmp_path)
