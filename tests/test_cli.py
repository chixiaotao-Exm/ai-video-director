from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from avd import cli
from avd.config import Settings
from avd.director.agent import DirectorAgent
from avd.director.review import approve
from avd.jobs import JobStatus, Worker
from avd.llm import FakeProvider
from avd.runtime import Runtime
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Version
from avd.state.repo import ProjectRepo


@pytest.fixture
def cli_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Runtime]:
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url=None)
    monkeypatch.setenv("AVD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AVD_DATABASE_URL", settings.database_url)
    # Constructor settings stay deterministic even when a developer has a local .env.
    monkeypatch.setattr(cli, "Settings", lambda **values: Settings(_env_file=None, **values))
    runtime = Runtime(settings)
    try:
        yield runtime
    finally:
        runtime.close()


def success(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    assert cli.main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_module_entry_point_and_all_application_tables(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["scripts"]["avd"] == "avd.cli:main"
    env = dict(os.environ)
    env["AVD_DATABASE_URL"] = f"sqlite:///{(tmp_path / 'state.db').as_posix()}"
    completed = subprocess.run(
        [sys.executable, "-m", "avd.cli", "--data-dir", str(tmp_path), "init"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=root,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "initialized"
    assert (tmp_path / "uploads").is_dir()
    with sqlite3.connect(tmp_path / "state.db") as database:
        tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "projects",
        "nodes",
        "versions",
        "characters",
        "voices",
        "jobs",
        "cost_entries",
        "chat_messages",
        "director_reviews",
    } <= tables


def test_init_projects_and_status(cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]) -> None:
    for _ in range(2):
        assert success(capsys, "init")["status"] == "initialized"
    source = cli_runtime.settings.data_dir / "uploads" / "reference.mp4"
    source.write_bytes(b"reference")
    created = success(
        capsys,
        "project",
        "create",
        "--title",
        "First",
        "--budget",
        "12.5",
        "--source-relative",
        "uploads/reference.mp4",
    )
    assert created["budget_limit"] == 12.5
    assert created["character_id"] is None and created["voice_id"] is None
    assert created["source_video_path"] == "uploads/reference.mp4"
    second = success(capsys, "project", "create", "--title", "Second")
    assert [item["id"] for item in success(capsys, "project", "list")] == [
        created["id"],
        second["id"],
    ]
    status = success(capsys, "project", "status", created["id"])
    assert status["nodes"] == []
    assert status["budget"]["remaining"] == 12.5


@pytest.mark.parametrize("budget", ["-1", "nan", "inf", "oops"])
def test_invalid_budget_has_readable_error(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], budget: str
) -> None:
    assert cli.main(["project", "create", "--title", "Bad", "--budget", budget]) == 2
    error = capsys.readouterr().err
    assert "budget" in error and "Traceback" not in error


def test_parser_errors_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["unknown-command"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert cli.main(["--help"]) == 0
    assert "worker" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["project", "status", "missing"],
        ["versions", "missing", "node"],
        ["revert", "missing", "node", "1"],
        ["jobs", "missing"],
        ["rebuild", "missing"],
        ["chat", "missing", "--message", "Hello"],
        ["analyze", "video.mp4", "--project", "missing"],
    ],
)
def test_unknown_projects_fail_without_tracebacks(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert cli.main(args) == 1
    result = capsys.readouterr()
    assert not result.out and "not found" in result.err
    assert "Traceback" not in result.err


def test_assets_are_unconsented_until_explicit_confirmation(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    uploads = cli_runtime.settings.data_dir / "uploads"
    uploads.mkdir()
    (uploads / "front.png").write_bytes(b"image")
    (uploads / "voice.wav").write_bytes(b"audio")
    character = success(
        capsys,
        "asset",
        "character",
        "create",
        "--name",
        "Character",
        "--front",
        "uploads/front.png",
        "--appearance-prompt",
        "A fictional character",
    )
    voice = success(
        capsys,
        "asset",
        "voice",
        "create",
        "--name",
        "Voice",
        "--sample",
        "uploads/voice.wav",
        "--engine",
        "local",
        "--speed",
        "1.1",
    )
    for kind, asset in [("character", character), ("voice", voice)]:
        assert asset["consent_confirmed"] is False
        confirmed = success(
            capsys, "consent", "confirm", kind, asset["id"], "--confirmed-by", "Owner"
        )
        assert confirmed["consent_confirmed"] is True
        assert confirmed["consent_confirmed_by"] == "Owner"
        assert confirmed["consent_confirmed_at"]
    assert voice["default_params"]["speed"] == 1.1
    assert character["reference_images"]["front"].startswith("assets/characters/")
    assert (
        cli.main(["consent", "confirm", "voice", character["id"], "--confirmed-by", "Owner"]) == 1
    )
    assert "Asset was not found" in capsys.readouterr().err


@pytest.mark.parametrize(
    "path",
    [
        "../outside.png",
        "uploads/../../outside.png",
        "C:/outside.png",
        "/outside.png",
        "assets/outside.png",
    ],
)
def test_asset_paths_are_confined_to_uploads(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], path: str
) -> None:
    assert (
        cli.main(
            [
                "asset",
                "character",
                "create",
                "--name",
                "Bad",
                "--front",
                path,
                "--appearance-prompt",
                "Test",
            ]
        )
        == 1
    )
    assert "Traceback" not in capsys.readouterr().err
    with cli_runtime.session_factory() as session:
        assert session.query(Character).count() == 0


def test_symlinked_upload_is_rejected(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    root = cli_runtime.settings.data_dir
    (root / "uploads").mkdir()
    (root / "outside.png").write_bytes(b"outside")
    try:
        (root / "uploads" / "ref.png").symlink_to(root / "outside.png")
    except OSError:
        pytest.skip("Creating symlinks requires permission on this system")
    assert (
        cli.main(
            [
                "asset",
                "character",
                "create",
                "--name",
                "Bad",
                "--front",
                "uploads/ref.png",
                "--appearance-prompt",
                "Test",
            ]
        )
        == 1
    )
    assert "links" in capsys.readouterr().err


def test_analyze_copies_and_queues_without_a_provider(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    video = cli_runtime.settings.data_dir / "source.mp4"
    video.write_bytes(b"local video")
    result = success(capsys, "analyze", str(video))
    assert result["status"] == "queued"
    assert (
        cli_runtime.settings.data_dir / result["source_video_path"]
    ).read_bytes() == video.read_bytes()
    job = cli_runtime.queue.get(result["job_id"])
    assert job.job_type == "analyze_video" and job.project_id == result["project_id"]
    assert job.status == JobStatus.QUEUED and job.attempts == 0
    assert success(capsys, "jobs", result["project_id"])[0]["id"] == job.id
    other = success(capsys, "project", "create", "--title", "Existing")
    existing = success(capsys, "analyze", str(video), "--project", other["id"])
    assert existing["project_id"] == other["id"]
    assert cli.main(["analyze", str(video), "--project", other["id"]]) == 1
    assert "pending analysis" in capsys.readouterr().err
    assert len(cli_runtime.queue.list(project_id=other["id"])) == 1


def test_versions_revert_and_jobs_are_project_scoped(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    with cli_runtime.session_factory() as session:
        repo = ProjectRepo(session)
        project = repo.create_project("First")
        other = repo.create_project("Other")
        node = repo.add_node(project, NodeKind.SCRIPT, recipe={"line": "first"})
        downstream = repo.add_node(project, NodeKind.VOICEOVER, depends_on=[node])
        first = repo.record_version(node, {}, note="first")
        repo.update_recipe(node, {"line": "second"})
        second = repo.record_version(node, {}, note="second")
        repo.record_version(downstream, {})
        session.commit()
        project_id, other_id, node_id = project.id, other.id, node.id
        downstream_id, first_id, second_id = downstream.id, first.id, second.id
    cli_runtime.queue.submit("cpu", "test", {}, project_id=other_id)
    assert success(capsys, "jobs", project_id) == []
    assert len(success(capsys, "jobs", other_id)) == 1
    assert [item["number"] for item in success(capsys, "versions", project_id, node_id)] == [1, 2]
    for command in [["versions", other_id, node_id], ["revert", other_id, node_id, "1"]]:
        assert cli.main(command) == 1
        assert "Node was not found in this project" in capsys.readouterr().err
    restored = success(capsys, "revert", project_id, node_id, "1")
    assert restored["id"] == first_id
    with cli_runtime.session_factory() as session:
        assert session.get(Node, node_id).current_version_id == first_id
        assert session.get(Node, downstream_id).status == NodeStatus.DIRTY
        assert session.get(Version, second_id).note == "second"


def test_rebuild_preserves_checkpoint_and_consent_gates(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str]
) -> None:
    with cli_runtime.session_factory() as session:
        repo = ProjectRepo(session)
        project = repo.create_project("Build", budget_limit=10)
        analysis = repo.add_node(project, NodeKind.ANALYSIS)
        graph = repo.create_shot_graph(project, 1)
        repo.record_version(analysis, {})
        repo.record_version(graph["script"], {})
        character = Character(name="C", consent_confirmed=True)
        session.add(character)
        project.character = character
        session.commit()
        project_id = project.id
    assert cli.main(["rebuild", project_id]) == 1
    assert "approval required" in capsys.readouterr().err.lower()
    assert cli_runtime.queue.list(project_id=project_id) == []
    with cli_runtime.session_factory() as session:
        project = session.get(Project, project_id)
        approve(session, project, "structure", "Owner")
        approve(session, project, "script", "Owner")
        session.commit()
    queued = success(capsys, "rebuild", project_id, "--retry-failed")
    assert len(queued["job_ids"]) == 1
    assert cli_runtime.queue.get(queued["job_ids"][0]).job_type == "image_gen"


def test_chat_single_message_and_repl_use_fake_provider(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = success(capsys, "project", "create", "--title", "Chat")
    provider = FakeProvider(["Hello from the director", "Second reply"])
    monkeypatch.setattr(
        cli, "Runtime", lambda settings: Runtime(settings, providers={"anthropic": provider})
    )
    assert (
        success(capsys, "chat", project["id"], "--message", "Hello")["reply"]
        == "Hello from the director"
    )
    messages = iter(["", "Continue", "/quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(messages))
    assert cli.main(["chat", project["id"]]) == 0
    assert "Director: Second reply" in capsys.readouterr().out
    assert len(provider.calls) == 2
    assert len(DirectorAgent(cli_runtime).history(project["id"])) == 4


def test_worker_once_uses_runtime_registry_and_completion_callback(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = Runtime.build_registry

    def registry(runtime: Runtime) -> Any:
        handlers = original(runtime)
        handlers.register("offline_test", lambda job: {"done": True})
        return handlers

    monkeypatch.setattr(
        Runtime,
        "build_worker",
        lambda runtime, lane: Worker(runtime.queue, lane, registry(runtime), runtime.on_finished),
    )
    monkeypatch.setattr(Runtime, "on_finished", lambda runtime, job: calls.append(job.id))
    job_id = cli_runtime.queue.submit("api", "offline_test", {})
    assert success(capsys, "worker", "--lane", "cpu", "--once")["worked"] is False
    assert success(capsys, "worker", "--lane", "api", "--once")["worked"] is True
    assert cli_runtime.queue.get(job_id).status == JobStatus.SUCCEEDED
    assert calls == [job_id]
    stale_id = cli_runtime.queue.submit("cpu", "offline_test", {}, max_attempts=1)
    cli_runtime.queue.claim_next("cpu")
    recovered = success(capsys, "worker", "--lane", "cpu", "--once", "--recover-stale")
    assert recovered["recovered"] == 1
    assert cli_runtime.queue.get(stale_id).status == JobStatus.FAILED


def test_serve_uses_requested_bind_without_opening_network(
    cli_runtime: Runtime, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("fastapi")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))
    assert cli.main(["serve", "--host", "127.0.0.2", "--port", "8123"]) == 0
    assert calls == [{"host": "127.0.0.2", "port": 8123}]
    assert not capsys.readouterr().out
