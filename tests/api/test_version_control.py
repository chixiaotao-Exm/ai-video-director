"""Version-center tests use temporary local repositories and stubbed Git failures."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from avd.api import versions_control as versions


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if not shutil.which("git"):
        pytest.skip("Git executable is unavailable")
    root = tmp_path / "repository"
    root.mkdir()
    assert versions._git(root, "init", "-b", "main") is not None
    assert versions._git(root, "config", "user.name", "Offline test") is not None
    assert versions._git(root, "config", "user.email", "test@example.invalid") is not None
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    assert versions._git(root, "add", "README.md") is not None
    assert versions._git(root, "commit", "-m", "Initial offline revision") is not None
    monkeypatch.setattr(versions, "_repository_root", lambda: root)
    return root


def test_version_endpoint_reports_local_commits_and_tags(repository: Path):
    versions._git(repository, "tag", "v0.1.0")
    versions._git(repository, "remote", "add", "origin", versions.REPOSITORY_URL + ".git")
    app = FastAPI()
    versions.install_version_routes(app)
    with TestClient(app) as client:
        response = client.get("/version-control")
        assert response.status_code == 200
        assert client.post("/version-control").status_code == 405
    data = response.json()
    assert data["available"] is True
    assert data["branch"] == "main"
    assert data["package_version"] == versions.version("avd")
    assert data["current_tag"] == "v0.1.0"
    assert data["configured_remote"] == versions.REPOSITORY_URL
    assert data["remote_matches_repository"] is True
    assert data["dirty"] is False
    assert data["changes_available"] is True
    assert data["recent_commits"][0]["subject"] == "Initial offline revision"
    assert data["recent_commits"][0]["hash"] == data["head"]
    assert data["tags"][0]["name"] == "v0.1.0"
    assert data["sync"]["status"] == "no_upstream"
    assert data["sync"]["source"] == "local_refs"
    assert str(repository) not in response.text
    assert "test@example.invalid" not in response.text


def test_dirty_counts_omit_names_and_private_untracked_data(repository: Path):
    (repository / "README.md").write_text("updated\n", encoding="utf-8")
    (repository / "fresh.txt").write_text("new", encoding="utf-8")
    (repository / "staged.txt").write_text("staged", encoding="utf-8")
    versions._git(repository, "add", "staged.txt")
    for private in ("data", ".venv", "node_modules"):
        folder = repository / private
        folder.mkdir()
        (folder / "secret.txt").write_text("private", encoding="utf-8")
    (repository / ".env").write_text("TOKEN=secret", encoding="utf-8")
    result = versions.version_control_status()
    assert result.dirty is True
    assert result.changes.model_dump() == {
        "tracked": 2,
        "staged": 1,
        "unstaged": 1,
        "untracked": 1,
        "conflicted": 0,
    }
    serialized = result.model_dump_json()
    assert "secret" not in serialized
    assert "fresh.txt" not in serialized
    assert "README.md" not in serialized


def test_rename_and_conflicts_are_counted_as_single_changes():
    result = versions._changes("R  new name.py\0old\nname.py\0UU conflict.py\0?? new.py\0")
    assert result.tracked == 2
    assert result.staged == 2
    assert result.unstaged == 1
    assert result.conflicted == 1
    assert result.untracked == 1


def test_current_sync_uses_existing_remote_ref_without_fetch(repository: Path):
    versions._git(repository, "remote", "add", "origin", versions.REPOSITORY_URL + ".git")
    versions._git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    versions._git(repository, "branch", "--set-upstream-to=origin/main")
    result = versions.version_control_status()
    assert result.sync.model_dump() == {
        "source": "local_refs",
        "status": "current",
        "upstream": "origin/main",
        "ahead": 0,
        "behind": 0,
    }
    versions._git(repository, "commit", "--allow-empty", "-m", "Local only")
    result = versions.version_control_status()
    assert result.sync.status == "ahead"
    assert result.sync.ahead == 1
    assert result.sync.behind == 0


@pytest.mark.parametrize(
    ("counts", "status"), [("0 2", "behind"), ("2 4", "diverged"), ("error", "unavailable")]
)
def test_sync_states_from_local_refs(tmp_path: Path, monkeypatch, counts: str, status: str):
    def fake_git(root, *args):
        return "origin/main" if args[0] == "rev-parse" else counts

    monkeypatch.setattr(versions, "_git", fake_git)
    result = versions._sync(tmp_path, True)
    assert result.status == status


def test_detached_head_still_has_history(repository: Path):
    versions._git(repository, "checkout", "--detach", "HEAD")
    result = versions.version_control_status()
    assert result.available is True
    assert result.branch is None
    assert result.head is not None
    assert len(result.recent_commits) == 1


def test_empty_repository_is_supported(tmp_path: Path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("Git executable is unavailable")
    assert versions._git(tmp_path, "init", "-b", "main") is not None
    monkeypatch.setattr(versions, "_repository_root", lambda: tmp_path)
    result = versions.version_control_status()
    assert result.available is True
    assert result.branch == "main"
    assert result.head is None
    assert result.recent_commits == []
    assert result.sync.status == "no_commits"


def test_installed_distribution_without_git_is_graceful(monkeypatch):
    monkeypatch.setattr(versions, "_repository_root", lambda: None)

    def unexpected_git(*args):
        pytest.fail("Git must not run outside the source repository")

    monkeypatch.setattr(versions, "_git", unexpected_git)
    result = versions.version_control_status()
    assert result.available is False
    assert result.package_version
    assert result.reason
    assert result.repository_url == versions.REPOSITORY_URL


def test_missing_package_metadata_does_not_invent_version(monkeypatch):
    monkeypatch.setattr(versions, "_repository_root", lambda: None)

    def missing_version(package):
        raise versions.PackageNotFoundError(package)

    monkeypatch.setattr(versions, "version", missing_version)
    assert versions.version_control_status().package_version is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://token:secret@github.com/chixiaotao-Exm/ai-video-director.git?token=private#fragment",
        "ssh://git@github.com/chixiaotao-Exm/ai-video-director.git",
        "git@github.com:chixiaotao-Exm/ai-video-director.git",
        versions.REPOSITORY_URL,
    ],
)
def test_remote_url_sanitizes_credentials(raw: str):
    assert versions._safe_remote_url(raw) == versions.REPOSITORY_URL


def test_actual_remote_credentials_do_not_reach_the_response(repository: Path):
    versions._git(
        repository,
        "remote",
        "add",
        "origin",
        "https://my-user:my-secret@github.com/chixiaotao-Exm/ai-video-director.git?token=private",
    )
    result = versions.version_control_status()
    assert result.configured_remote == versions.REPOSITORY_URL
    assert "my-user" not in result.model_dump_json()
    assert "my-secret" not in result.model_dump_json()
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize(
    "raw",
    [
        "file:///private/path",
        "https://git.internal/private-project",
        "https://github.com.evil.example/owner/repo",
        "https://github.com/owner/repo/secret",
        "https://github.com/../repo",
        "https://github.com/owner/repo\nsecret",
        "https://[invalid/owner/repo",
        None,
    ],
)
def test_remote_urls_do_not_expose_private_paths(raw):
    assert versions._safe_remote_url(raw) is None


@pytest.mark.parametrize(
    "failure", [FileNotFoundError("private"), subprocess.TimeoutExpired("git", 3)]
)
def test_git_failures_are_sanitized(tmp_path: Path, monkeypatch, failure):
    monkeypatch.setattr(versions, "_repository_root", lambda: tmp_path)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    result = versions.version_control_status()
    assert result.available is False
    assert "private" not in result.model_dump_json()


def test_subprocess_is_bounded_and_ignores_ambient_git_overrides(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/private/another/repository")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")

    def run(command, **kwargs):
        assert isinstance(command, list)
        assert command[0] == "git"
        assert "core.fsmonitor=false" in command
        assert "--no-optional-locks" in command
        assert "--no-lazy-fetch" in command
        assert kwargs["cwd"] == tmp_path
        assert kwargs["timeout"] == 3
        assert "shell" not in kwargs
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_CONFIG_COUNT" not in kwargs["env"]
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        return subprocess.CompletedProcess(command, 1, "", "credential secret")

    monkeypatch.setattr(subprocess, "run", run)
    assert versions._git(tmp_path, "status") is None


def test_api_has_no_client_control_over_paths_or_git_arguments(repository: Path, monkeypatch):
    commands = []
    original = versions._git

    def record(root, *args):
        commands.append(args)
        return original(root, *args)

    monkeypatch.setattr(versions, "_git", record)
    app = FastAPI()
    versions.install_version_routes(app)
    with TestClient(app) as client:
        result = client.get("/version-control?path=C:/private&command=fetch")
    assert result.status_code == 200
    assert "fetch" not in json.dumps(commands)
    assert "C:/private" not in json.dumps(commands)
    assert {command[0] for command in commands} <= {
        "rev-parse",
        "symbolic-ref",
        "remote",
        "status",
        "log",
        "describe",
        "for-each-ref",
        "rev-list",
    }


def test_commits_and_tags_are_limited():
    sha = "a" * 40
    records = "\n".join(
        "\0".join((sha, "aaaaaaa", "2026-10-20T00:00:00Z", f"Commit {i}")) for i in range(25)
    )
    assert len(versions._commits(records)) == versions.MAX_COMMITS
    tags = "\n".join("\0".join((f"v0.1.{i}", sha, "2026-10-20T00:00:00Z")) for i in range(40))
    assert len(versions._tags(tags)) == versions.MAX_TAGS


def test_status_failure_is_not_reported_as_a_verified_clean_tree(repository: Path, monkeypatch):
    original = versions._git

    def fail_status(root, *args):
        return None if args[0] == "status" else original(root, *args)

    monkeypatch.setattr(versions, "_git", fail_status)
    result = versions.version_control_status()
    assert result.available is True
    assert result.changes_available is False


def test_checkout_detection_does_not_search_parent_directories(tmp_path: Path, monkeypatch):
    (tmp_path / ".git").mkdir()
    wheel_file = tmp_path / "venv" / "Lib" / "site-packages" / "avd" / "api" / "versions_control.py"
    monkeypatch.setattr(versions, "__file__", str(wheel_file))
    assert versions._repository_root() is None
