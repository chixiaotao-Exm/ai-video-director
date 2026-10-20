from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from avd import demo
from avd.backends.media import MediaProcessingError
from avd.config import Settings
from avd.jobs import Job, JobLane, JobStatus
from avd.state.db import create_engine, create_session_factory
from avd.state.models import NodeKind, NodeStatus, Project


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf"), True])
def test_demo_rejects_invalid_duration_before_creating_files(
    tmp_path: Path, duration: float
) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        demo.run_demo(tmp_path / "demo", duration_s=duration)
    assert list(tmp_path.iterdir()) == []


def test_demo_requires_both_media_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo.shutil, "which", lambda name: None)
    with pytest.raises(MediaProcessingError, match="ffmpeg, ffprobe"):
        demo.run_demo(tmp_path / "demo")
    assert list(tmp_path.iterdir()) == []


def test_card_draws_literal_text_from_file_in_safe_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[tuple[list[str], Path | None]] = []

    def capture(arguments: list[str], *, cwd: Path | None = None) -> str:
        commands.append((arguments, cwd))
        Path(arguments[-1]).write_bytes(b"png")
        return ""

    monkeypatch.setattr(demo, "_run", capture)
    text = "It's 100% literal: %{metadata:title}; [bad] & $(whoami)"
    destination = tmp_path / "path with spaces and ' quotes" / "frame.png"
    demo._card(text, destination)
    arguments, cwd = commands[0]
    assert cwd == destination.parent
    assert (cwd / "frame.txt").read_text(encoding="utf-8") == text
    expression = arguments[arguments.index("-vf") + 1]
    assert "textfile=frame.txt:expansion=none" in expression
    assert text not in expression
    assert str(destination.parent) not in expression
    assert arguments[:6] == ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
    with pytest.raises(FileExistsError):
        demo._card("changed", destination)


def test_demo_tts_uses_matching_storyboard_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def capture(arguments: list[str], *, cwd: Path | None = None) -> str:
        commands.append(arguments)
        Path(arguments[-1]).write_bytes(b"wav")
        return ""

    monkeypatch.setattr(demo, "_run", capture)
    monkeypatch.setattr(demo, "media_duration", lambda path: 1.75)
    storyboard = demo._storyboard(1.75).model_dump(mode="json")
    storyboard["shots"][0]["duration_s"] = 0.5
    job = Job(
        job_type="tts",
        payload={
            "data_dir": str(tmp_path),
            "output_dir": "projects/demo/voiceover/1",
            "recipe": {"shot_index": 2},
            "context": {"storyboard": storyboard},
        },
    )
    handler = demo.build_demo_registry(Settings(_env_file=None, data_dir=tmp_path)).get("tts")
    assert handler is not None
    result = handler(job)
    assert result["duration_s"] == 1.75
    assert commands[0][commands[0].index("-t") + 1] == "1.75"
    assert result["outputs"]["audio"] == "projects/demo/voiceover/1/attempt-1/voice.wav"


def test_main_reports_missing_ffmpeg_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(demo.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit) as error:
        demo.main(["--data-dir", str(tmp_path / "media")])
    assert error.value.code == 1
    assert "Offline demo requires ffmpeg, ffprobe on PATH" in capsys.readouterr().err


def test_main_accepts_configured_default_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path | None] = []

    def fake_run(data_dir: Path | None) -> dict[str, Any]:
        calls.append(data_dir)
        return {}

    monkeypatch.setattr(demo, "run_demo", fake_run)
    assert demo.main([]) == 0
    assert calls == [None]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="FFmpeg and ffprobe are required for the real-media offline demo",
)
def test_offline_demo_real_mp4_incremental_versions_and_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "media with spaces"
    messages: list[str] = []
    external_database = tmp_path / "existing-application-state.db"
    external_database.write_bytes(b"application state must not be opened")
    monkeypatch.setenv("AVD_DATABASE_URL", f"sqlite:///{external_database.as_posix()}")
    monkeypatch.setenv("AVD_JOB_ESTIMATED_COSTS", '{"image_gen": 100}')
    monkeypatch.setenv("AVD_DATA_DIR", str(root))
    report = demo.run_demo(duration_s=0.5, output=messages.append)

    final = Path(report["final_path"])
    assert final == root / "demo" / "final.mp4"
    assert final.is_file() and final.stat().st_size > 0
    assert final.read_bytes() == Path(report["version_final_path"]).read_bytes()
    assert Path(report["initial_final_path"]).is_file()
    assert report["initial_final_path"] != report["version_final_path"]
    assert report["shot_count"] == 3
    assert len(report["initial_nodes"]) == 16
    assert report["rerun_nodes"] == ["voiceover[2]", "lipsync[2]", "subtitles[2]", "final"]
    assert set(report["cache_hit_nodes"]) == set(report["rerun_nodes"])
    assert report["cache_hits"] == 4
    assert report["job_count"] == 20
    assert external_database.read_bytes() == b"application state must not be opened"
    assert any("16 generated nodes" in message for message in messages)
    assert any("4 reran" in message for message in messages)
    assert any("4 cache hits" in message for message in messages)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(final)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    metadata = json.loads(probe.stdout)
    assert metadata["format"]["tags"]["comment"] == "AI-generated"
    assert 1.4 <= float(metadata["format"]["duration"]) < 2.0
    streams = {stream["codec_type"]: stream for stream in metadata["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (640, 360)
    assert streams["audio"]["codec_name"] == "aac"

    database = create_engine(f"sqlite:///{Path(report['database_path']).as_posix()}")
    try:
        with create_session_factory(database)() as session:
            project = session.get(Project, report["project_id"])
            assert project is not None
            assert project.spent == 0
            assert project.character is not None and project.voice is not None
            for asset in (project.character, project.voice):
                assert asset.consent_confirmed
                assert "synthetic assets" in str(asset.consent_confirmed_by)
                assert asset.consent_confirmed_at is not None
            assert len(project.nodes) == 17
            assert {node.shot_index for node in project.nodes if node.shot_index} == {1, 2, 3}
            for node in project.nodes:
                assert node.status == NodeStatus.CLEAN
                versions = sorted(node.versions, key=lambda item: item.number)
                label = node.kind.value + (f"[{node.shot_index}]" if node.shot_index else "")
                expected_count = 2 if label in report["rerun_nodes"] or label == "script" else 1
                assert len(versions) == expected_count
                assert node.current_version_id == versions[-1].id
                for version in versions:
                    for relative in version.output_paths.values():
                        assert not Path(relative).is_absolute()
                        assert (root / relative).is_file()
                        if node.kind != NodeKind.SCRIPT:
                            assert relative.startswith(f"projects/{project.id}/{node.id}/")
                if node.kind == NodeKind.SUBTITLES and node.shot_index == 2:
                    first = (root / versions[0].output_paths["srt"]).read_text(encoding="utf-8")
                    last = (root / versions[1].output_paths["srt"]).read_text(encoding="utf-8")
                    assert "Change one line" in first
                    assert demo._EDITED_LINE in last
                if node.kind == NodeKind.FINAL:
                    assert root / versions[0].output_paths["final"] == Path(
                        report["initial_final_path"]
                    )
                    assert root / versions[1].output_paths["final"] == Path(
                        report["version_final_path"]
                    )
            jobs = list(session.scalars(select(Job).where(Job.project_id == project.id)))
            assert len(jobs) == 20  # No work was enqueued for the four cache hits.
            assert all(job.status == JobStatus.SUCCEEDED and job.attempts == 1 for job in jobs)
            assert {job.lane for job in jobs} == {JobLane.API, JobLane.GPU, JobLane.CPU}
    finally:
        database.dispose()
