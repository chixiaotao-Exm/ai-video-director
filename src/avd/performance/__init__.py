"""Deterministic preparation for reference-driven singing and dancing projects."""

from .models import PerformanceConfig, PreparationRequest, PreparedPerformance
from .prepare import MediaTools, PerformanceMediaError, prepare_performance, probe_source

__all__ = [
    "MediaTools",
    "PerformanceConfig",
    "PerformanceMediaError",
    "PreparationRequest",
    "PreparedPerformance",
    "prepare_performance",
    "probe_source",
]
