"""Deterministic, injectable handlers for the persistent job queue."""

from .handlers import build_registry

__all__ = ["build_registry"]
