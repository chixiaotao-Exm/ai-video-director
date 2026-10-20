"""Durable queue operations with atomic job claiming."""

from __future__ import annotations

import copy
import math
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, text, update
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


class ClaimLostError(ValueError):
    """The worker no longer owns the attempt it is trying to update."""


class JobQueue:
    """Persist and claim jobs using a caller-provided session factory."""

    def __init__(
        self, session_factory: Callable[[], Session], *, lease_seconds: float = 30.0
    ) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be finite and greater than zero")
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds

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
        session: Session | None = None,
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
        if session is not None:
            session.add(job)
            session.flush()
            return job.id
        with self.session_factory() as owned_session:
            owned_session.add(job)
            owned_session.flush()
            job_id = job.id
            owned_session.commit()
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
                .returning(Job.id)
            )
            cancelled = result.scalar_one_or_none() is not None
            session.commit()
            return cancelled

    def retry(self, job_id: str, *, additional_attempts: int = 3) -> Job:
        """Give a failed/cancelled job more tries without reusing attempt numbers."""

        if additional_attempts < 1:
            raise ValueError("additional_attempts must be at least 1")
        with self.session_factory() as session:
            self._begin_write_transaction(session)
            job = session.get(Job, job_id)
            if job is None:
                raise KeyError(f"Job {job_id} was not found")
            if job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                raise ValueError(f"Job {job_id} is not failed or cancelled")
            job.status = JobStatus.QUEUED
            job.max_attempts = job.attempts + additional_attempts
            job.error = None
            job.result = None
            job.started_at = None
            job.finished_at = None
            job.claim_token = None
            job.lease_expires_at = None
            return self._commit_and_detach(session, job)

    def claim_next(
        self,
        lane: JobLane | str,
        current_model_key: str | None = None,
    ) -> Job | None:
        requested_lane = JobLane(lane)
        with self.session_factory() as session:
            self._begin_write_transaction(session)
            # The occupancy check and claim share SQLite's write transaction, so
            # workers in separate processes cannot occupy the GPU lane together.
            if requested_lane == JobLane.GPU:
                running_gpu = session.scalar(
                    select(Job.id)
                    .where(Job.lane == JobLane.GPU, Job.status == JobStatus.RUNNING)
                    .limit(1)
                )
                if running_gpu is not None:
                    session.rollback()
                    return None
            candidates = list(
                session.scalars(
                    select(Job).where(
                        Job.lane == requested_lane,
                        Job.status == JobStatus.QUEUED,
                        Job.attempts < Job.max_attempts,
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
            job.claim_token = str(uuid.uuid4())
            job.lease_expires_at = job.started_at + timedelta(seconds=self.lease_seconds)
            return self._commit_and_detach(session, job)

    def heartbeat(self, job_id: str, claim_token: str) -> bool:
        """Renew a running attempt's lease; return False if ownership was lost."""

        with self.session_factory() as session:
            renewed = (
                session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.status == JobStatus.RUNNING,
                        Job.claim_token == claim_token,
                    )
                    .values(lease_expires_at=_utc_now() + timedelta(seconds=self.lease_seconds))
                    .returning(Job.id)
                ).scalar_one_or_none()
                is not None
            )
            session.commit()
            return renewed

    def complete(
        self,
        job_id: str,
        result: dict[str, Any],
        *,
        claim_token: str | None = None,
    ) -> Job:
        with self.session_factory() as session:
            self._begin_write_transaction(session)
            job = self._get_running(session, job_id, claim_token)
            job.status = JobStatus.SUCCEEDED
            job.result = copy.deepcopy(result)
            job.error = None
            job.finished_at = _utc_now()
            job.claim_token = None
            job.lease_expires_at = None
            return self._commit_and_detach(session, job)

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        retry: bool = True,
        claim_token: str | None = None,
    ) -> Job:
        with self.session_factory() as session:
            self._begin_write_transaction(session)
            job = self._get_running(session, job_id, claim_token)
            should_retry = retry and job.attempts < job.max_attempts
            job.error = error
            job.result = None
            job.claim_token = None
            job.lease_expires_at = None
            if should_retry:
                job.status = JobStatus.QUEUED
                job.started_at = None
                job.finished_at = None
            else:
                job.status = JobStatus.FAILED
                job.finished_at = _utc_now()
            return self._commit_and_detach(session, job)

    def recover_stale(self, *, force: bool = False) -> int:
        """Recover expired leases; force only after all prior workers have stopped.

        Legacy running rows with no lease are also stale. Active leases are left
        alone unless the caller explicitly confirms exclusive recovery via force.
        """

        with self.session_factory() as session:
            self._begin_write_transaction(session)
            recovered_at = _utc_now()
            statement = select(Job).where(Job.status == JobStatus.RUNNING)
            if not force:
                statement = statement.where(
                    or_(Job.lease_expires_at <= recovered_at, Job.lease_expires_at.is_(None))
                )
            stale_jobs = list(session.scalars(statement).all())
            for job in stale_jobs:
                job.started_at = None
                job.claim_token = None
                job.lease_expires_at = None
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
    def _begin_write_transaction(session: Session) -> None:
        bind = session.get_bind()
        if bind.dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.begin()

    @staticmethod
    def _get_running(session: Session, job_id: str, claim_token: str | None) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise KeyError(f"Job {job_id} was not found")
        if claim_token is not None and (
            job.status != JobStatus.RUNNING or job.claim_token != claim_token
        ):
            raise ClaimLostError(f"Job {job_id} is no longer owned by this attempt")
        if job.status != JobStatus.RUNNING:
            raise ValueError(f"Job {job_id} is not running")
        return job

    @staticmethod
    def _commit_and_detach(session: Session, job: Job) -> Job:
        session.flush()
        # Capture the attempt while still holding the write transaction. Detach
        # before commit so even expire_on_commit=True preserves that snapshot.
        session.refresh(job)
        session.expunge(job)
        session.commit()
        return job


__all__ = ["ClaimLostError", "JobQueue"]
