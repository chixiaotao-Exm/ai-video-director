"""Budget held by active jobs and completed executor attempts awaiting accounting."""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.jobs.models import Job, JobStatus

from .ledger import CostEntry

_AUTHORING_JOBS = frozenset({"analyze_video", "write_script"})


def _cost(value: Any) -> float:
    cost = float(value)
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Reserved cost must be finite and non-negative")
    return cost


def _paid_attempts(payload: dict[str, Any], attempts: int) -> set[int]:
    started = payload.get("_started_paid_attempts", [])
    if not isinstance(started, list):
        return set()
    return {value for value in started if type(value) is int and 0 < value <= attempts}


def reserved_cost(session: Session, project_id: str, *, exclude: str | None = None) -> float:
    """Keep costs reserved until the ledger and project spending include them.

    ``exclude`` removes only that job's active estimate, allowing its executor to
    check the imminent call separately. Earlier unaccounted attempts still count.
    Queued retries reserve their next call and every prior unaccounted paid call.
    Authoring jobs account text usage directly, so only their active work is held.
    """
    recorded = set(
        session.execute(
            select(CostEntry.job_id, CostEntry.attempt).where(
                CostEntry.project_id == project_id,
                CostEntry.job_id.is_not(None),
                CostEntry.attempt.is_not(None),
            )
        )
    )
    # Scalar columns reflect the current transaction even if a caller loaded the
    # Job object before another worker completed it; no stale identity-map values.
    jobs = session.execute(
        select(
            Job.id,
            Job.node_id,
            Job.job_type,
            Job.payload,
            Job.result,
            Job.status,
            Job.attempts,
        ).where(Job.project_id == project_id)
    )
    costs: list[float] = []
    for job_id, node_id, job_type, payload, result, status, attempts in jobs:
        active = status in {JobStatus.QUEUED, JobStatus.RUNNING}
        latest_success = (
            status == JobStatus.SUCCEEDED
            and (node_id is not None or job_type == "character_sheet")
            and job_type not in _AUTHORING_JOBS
        )
        pending = _paid_attempts(payload, attempts)
        if status == JobStatus.RUNNING:
            pending.discard(attempts)
        if latest_success:
            # Successful fake/local handlers may not set the paid-call marker;
            # publication still records their result cost as an executor attempt.
            pending.add(attempts or 1)
        pending = {attempt for attempt in pending if (job_id, attempt) not in recorded}
        if not pending and not (active and job_id != exclude):
            continue
        estimate = _cost(payload.get("estimated_cost", 0))
        if active and job_id != exclude:
            costs.append(estimate)
        for attempt in pending:
            if latest_success and attempt == (attempts or 1):
                costs.append(_cost((result or {}).get("cost", estimate)))
            else:
                costs.append(estimate)
    return math.fsum(costs)


__all__ = ["reserved_cost"]
