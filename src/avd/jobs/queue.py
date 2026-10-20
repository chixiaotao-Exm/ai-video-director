"""Durable queue operations with atomic job claiming."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
import threading
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .models import Job, JobLane, JobStatus


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_CREATED_AT_LOCK = threading.Lock()
_LAST_CREATED_AT: datetime | None = None


def _next_created_at() -> datetime:
    """Return a process-wide monotonic enqueue timestamp."""

    global _LAST_CREATED_AT
    with _CREATED_AT_LOCK:
        current = _utc_now()
        if _LAST_CREATED_AT is not None and current <= _LAST_CREATED_AT:
            current = _LAST_CREATED_AT + timedelta(microseconds=1)
        _LAST_CREATED_AT = current
        return current


class JobQueue:
    """Persist and claim jobs using a caller-provided session factory."""

    def __init__(self, session_factory: Callable[[], Session]):
        self.session_factory = session_factory

    def submit(
        self,
        lane: JobLane | str,
        job_type: str,
        payload: dict[str, Any],
        *,
        model_key: str | None = None,
        project_id: str | None = None,
        node_id: str | None = None,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> str:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        job = Job(
            lane=JobLane(lane),
            job_type=job_type,
            model_key=model_key,
            project_id=project_id,
            node_id=node_id,
            payload=copy.deepcopy(payload),
            max_attempts=max_attempts,
            priority=priority,
            created_at=_next_created_at(),
        )
        with self.session_factory() as session:
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()
        return job_id

    def get(self, job_id: str) -> Job | None:
        with self.session_factory() as session:
            job = session.get(Job, job_id)
            if job is not None:
                session.expunge(job)
            return job

    def list(
        self,
        project_id: str | None = None,
        status: JobStatus | str | None = None,
    ) -> list[Job]:
        with self.session_factory() as session:
            statement = select(Job).order_by(Job.created_at, Job.id)
            if project_id is not None:
                statement = statement.where(Job.project_id == project_id)
            if status is not None:
                statement = statement.where(Job.status == JobStatus(status))
            jobs = list(session.scalars(statement).all())
            session.expunge_all()
            return jobs

    def cancel(self, job_id: str) -> bool:
        with self.session_factory() as session:
            result = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JobStatus.QUEUED)
                .values(status=JobStatus.CANCELLED, finished_at=_utc_now())
            )
            session.commit()
            return result.rowcount == 1

    def claim_next(
        self,
        lane: JobLane | str,
        current_model_key: str | None = None,
    ) -> Job | None:
        requested_lane = JobLane(lane)
        with self.session_factory() as session:
            self._begin_claim_transaction(session)
            candidates = list(
                session.scalars(
                    select(Job).where(
                        Job.lane == requested_lane,
                        Job.status == JobStatus.QUEUED,
                    )
                ).all()
            )
            candidates.sort(
                key=lambda job: self._claim_sort_key(job, requested_lane, current_model_key)
            )
            if not candidates:
                session.rollback()
                return None
            job = candidates[0]
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.started_at = _utc_now()
            job.finished_at = None
            session.commit()
            session.expunge(job)
            return job

    def complete(self, job_id: str, result: dict[str, Any]) -> Job:
        with self.session_factory() as session:
            job = self._get_running(session, job_id)
            job.status = JobStatus.SUCCEEDED
            job.result = copy.deepcopy(result)
            job.error = None
            job.finished_at = _utc_now()
            session.commit()
            session.expunge(job)
            return job

    def fail(self, job_id: str, error: str, *, retry: bool = True) -> Job:
        with self.session_factory() as session:
            job = self._get_running(session, job_id)
            should_retry = retry and job.attempts < job.max_attempts
            job.error = error
            job.result = None
            if should_retry:
                job.status = JobStatus.QUEUED
                job.started_at = None
                job.finished_at = None
            else:
                job.status = JobStatus.FAILED
                job.finished_at = _utc_now()
            session.commit()
            session.expunge(job)
            return job

    def recover_stale(self) -> int:
        with self.session_factory() as session:
            stale_jobs = list(
                session.scalars(
                    select(Job).where(Job.status == JobStatus.RUNNING)
                ).all()
            )
            recovered_at = _utc_now()
            for job in stale_jobs:
                job.started_at = None
                if job.attempts >= job.max_attempts:
                    job.status = JobStatus.FAILED
                    job.error = "worker crashed"
                    job.finished_at = recovered_at
                else:
                    job.status = JobStatus.QUEUED
                    job.finished_at = None
            session.commit()
            return len(stale_jobs)

    @staticmethod
    def _claim_sort_key(
        job: Job,
        lane: JobLane,
        current_model_key: str | None,
    ) -> tuple[int, int, datetime, str]:
        model_preference = 0
        if lane == JobLane.GPU and current_model_key is not None:
            model_preference = int(job.model_key != current_model_key)
        return (-job.priority, model_preference, job.created_at, job.id)

    @staticmethod
    def _begin_claim_transaction(session: Session) -> None:
        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.begin()

    @staticmethod
    def _get_running(session: Session, job_id: str) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise KeyError(f"Job {job_id} was not found")
        if job.status != JobStatus.RUNNING:
            raise ValueError(f"Job {job_id} is not running")
        return job


__all__ = ["JobQueue"]
