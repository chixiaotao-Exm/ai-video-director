"""Optional HTTP interface; importing this package does not require FastAPI."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Load the optional API dependencies only when creating an application."""

    from .app import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["create_app"]
