"""Portable, data-relative paths shared by persistence and file storage."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


def relative_path(value: str) -> str:
    """Validate either platform's syntax and return a relative POSIX path."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Media must use a non-empty relative path under data_dir")
    portable = value.replace("\\", "/")
    windows = PureWindowsPath(value)
    parts = portable.split("/")
    if (
        portable.startswith("/")
        or windows.drive
        or ".." in parts
        or any(part not in {"", "."} and part != part.rstrip(" .") for part in parts)
        or ":" in portable
        or not any(part not in {"", "."} for part in parts)
    ):
        raise ValueError("Media must use a relative path under data_dir without traversal")
    return PurePosixPath(portable).as_posix()


def relative_paths(value: Any) -> Any:
    """Validate a JSON collection whose leaves are media paths."""

    if isinstance(value, str):
        return relative_path(value)
    if isinstance(value, dict):
        return {key: relative_paths(path) for key, path in value.items()}
    if isinstance(value, list):
        return [relative_paths(path) for path in value]
    raise ValueError("Media paths must be strings or collections of strings")
