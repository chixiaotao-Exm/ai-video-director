"""Read-only, local Git metadata for the browser's software version center."""

from __future__ import annotations

import os
import re
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI
from pydantic import BaseModel, Field

REPOSITORY_URL = "https://github.com/chixiaotao-Exm/ai-video-director"
GIT_TIMEOUT_SECONDS = 3
MAX_COMMITS = 15
MAX_TAGS = 30


class WorkingChanges(BaseModel):
    """Counts intentionally omit filenames, which may contain private asset names."""

    tracked: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    conflicted: int = 0


class CommitInfo(BaseModel):
    hash: str
    short_hash: str
    subject: str
    committed_at: str


class TagInfo(BaseModel):
    name: str
    hash: str
    created_at: str


class SyncInfo(BaseModel):
    source: str = "local_refs"
    status: str = "unavailable"
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None


class VersionControlInfo(BaseModel):
    available: bool = False
    reason: str | None = None
    package_version: str | None = None
    repository_url: str = REPOSITORY_URL
    configured_remote: str | None = None
    remote_matches_repository: bool = False
    branch: str | None = None
    head: str | None = None
    short_head: str | None = None
    current_tag: str | None = None
    dirty: bool = False
    changes_available: bool = False
    changes: WorkingChanges = Field(default_factory=WorkingChanges)
    recent_commits: list[CommitInfo] = Field(default_factory=list)
    tags: list[TagInfo] = Field(default_factory=list)
    sync: SyncInfo = Field(default_factory=SyncInfo)


def _repository_root() -> Path | None:
    """Only this source checkout is eligible, never a caller-supplied path or cwd."""
    source = Path(__file__).resolve()
    root = source.parents[3]
    if source != root / "src" / "avd" / "api" / "versions_control.py":
        return None
    return root if (root / ".git").exists() else None


def _git(root: Path, *arguments: str) -> str | None:
    # No lazy fetching in partial clones, credential prompts, optional index writes,
    # fsmonitor executables, submodule commands, or caller Git environment overrides.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    command = [
        "git",
        "--no-optional-locks",
        "--no-lazy-fetch",
        "-c",
        f"safe.directory={root.as_posix()}",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Never expose stderr: Git errors can contain local paths or remote credentials.
    return result.stdout.rstrip("\r\n") if result.returncode == 0 else None


def _safe_remote_url(raw: str | None) -> str | None:
    """Expose only a canonical GitHub repository URL, without any credentials."""
    if not raw or any(ord(char) < 32 for char in raw):
        return None
    if raw.startswith("git@github.com:"):
        raw = "https://github.com/" + raw.removeprefix("git@github.com:")
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"https", "http", "ssh", "git"}:
            return None
        if parsed.hostname != "github.com":
            return None
        path = parsed.path.strip("/").removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path):
            return None
        if any(part in {".", ".."} for part in path.split("/")):
            return None
    except ValueError:
        return None
    return f"https://github.com/{path}"


def _private_untracked(path: str) -> bool:
    parts = Path(path.replace("\\", "/")).parts
    if not parts:
        return False
    return (
        parts[0] in {"data", ".venv", ".git"}
        or "node_modules" in parts
        or any(
            part == ".env" or (part.startswith(".env.") and part != ".env.example")
            for part in parts
        )
    )


def _changes(porcelain: str) -> WorkingChanges:
    counts = WorkingChanges()
    records = iter(porcelain.split("\0"))
    for record in records:
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if "R" in status or "C" in status:
            next(records, None)  # With -z, a rename/copy's source is a separate record.
        if status == "??":
            if not _private_untracked(path):
                counts.untracked += 1
        elif status != "!!":
            counts.tracked += 1
            counts.staged += status[0] != " "
            counts.unstaged += status[1] != " "
            counts.conflicted += status in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}
    return counts


def _commits(output: str | None) -> list[CommitInfo]:
    result: list[CommitInfo] = []
    for record in (output or "").splitlines()[:MAX_COMMITS]:
        fields = record.split("\0", 3)
        if len(fields) == 4 and re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            result.append(
                CommitInfo(
                    hash=fields[0],
                    short_hash=fields[1],
                    committed_at=fields[2],
                    subject=fields[3][:200],
                )
            )
    return result


def _tags(output: str | None) -> list[TagInfo]:
    result: list[TagInfo] = []
    for record in (output or "").splitlines()[:MAX_TAGS]:
        fields = record.split("\0")
        if len(fields) == 3 and re.fullmatch(r"[0-9a-f]{40,64}", fields[1]):
            result.append(TagInfo(name=fields[0][:200], hash=fields[1], created_at=fields[2]))
    return result


def _sync(root: Path, has_head: bool) -> SyncInfo:
    if not has_head:
        return SyncInfo(status="no_commits")
    upstream = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream:
        return SyncInfo(status="no_upstream")
    output = _git(root, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
    parts = (output or "").split()
    if len(parts) != 2 or any(not value.isdigit() for value in parts):
        return SyncInfo(upstream=upstream[:200])
    ahead, behind = map(int, parts)
    status = (
        "diverged" if ahead and behind else "ahead" if ahead else "behind" if behind else "current"
    )
    return SyncInfo(status=status, upstream=upstream[:200], ahead=ahead, behind=behind)


def version_control_status() -> VersionControlInfo:
    result = VersionControlInfo()
    try:
        result.package_version = version("avd")
    except PackageNotFoundError:
        pass
    root = _repository_root()
    if root is None:
        result.reason = "此安装不包含 Git 仓库；应用功能不受影响。"
        return result
    top_level = _git(root, "rev-parse", "--show-toplevel")
    if not top_level or Path(top_level).resolve() != root.resolve():
        result.reason = "Git 不可用、仓库无法读取或查询超时。"
        return result
    result.available = True
    result.head = _git(root, "rev-parse", "--verify", "HEAD") or None
    result.short_head = result.head[:8] if result.head else None
    result.branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD") or None
    result.configured_remote = _safe_remote_url(_git(root, "remote", "get-url", "origin"))
    result.remote_matches_repository = (
        result.configured_remote or ""
    ).casefold() == REPOSITORY_URL.casefold()
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=normal",
        "--ignore-submodules=all",
    )
    result.changes_available = status is not None
    if status is not None:
        result.changes = _changes(status)
        result.dirty = bool(result.changes.tracked or result.changes.untracked)
    if result.head:
        result.recent_commits = _commits(
            _git(
                root, "log", f"--max-count={MAX_COMMITS}", "--format=%H%x00%h%x00%cI%x00%s", "HEAD"
            )
        )
        result.current_tag = _git(root, "describe", "--tags", "--exact-match", "HEAD") or None
    result.tags = _tags(
        _git(
            root,
            "for-each-ref",
            "--sort=-creatordate",
            f"--count={MAX_TAGS}",
            "--format=%(refname:short)%00%(objectname)%00%(creatordate:iso-strict)",
            "refs/tags",
        )
    )
    result.sync = _sync(root, bool(result.head))
    return result


def install_version_routes(app: FastAPI) -> None:
    @app.get("/version-control", response_model=VersionControlInfo)
    def get_version_control() -> VersionControlInfo:
        return version_control_status()


__all__ = ["install_version_routes", "version_control_status"]
