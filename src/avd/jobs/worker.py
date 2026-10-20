"""Background workers for executing queued jobs."""

from __future__ import annotations

import logging
import math
import threading
import traceback
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any

from avd.observability import log_context

from .models import Job, JobLane
from .process import JobRunner, ProcessCleanupError
from .queue import ClaimLostError, JobQueue

JobHandler = Callable[[Job], dict[str, Any]]
_LOGGER = logging.getLogger(__name__)


class HandlerRegistry:
    """Named job handlers used by workers."""

    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, job_type: str, handler: JobHandler) -> None:
        self._handlers[job_type] = handler

    def get(self, job_type: str) -> JobHandler | None:
        return self._handlers.get(job_type)


class Worker:
    """Claim and execute one lane of jobs."""

    def __init__(
        self,
        queue: JobQueue,
        lane: JobLane | str,
        registry: HandlerRegistry | MutableMapping[str, JobHandler],
        on_finished: Callable[[Job], None] | None = None,
        *,
        runner: JobRunner | None = None,
        timeout_seconds: float | None = None,
        job_timeouts: Mapping[str, float] | None = None,
        shutdown_grace_seconds: float = 30.0,
    ) -> None:
        timeouts = dict(job_timeouts or {})
        for value in [
            shutdown_grace_seconds,
            *timeouts.values(),
            *([] if timeout_seconds is None else [timeout_seconds]),
        ]:
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Worker deadlines must be positive finite seconds")
        if (timeout_seconds is not None or timeouts) and runner is None:
            raise ValueError("Hard timeouts require an isolated job runner")
        if runner is not None and timeout_seconds is None:
            raise ValueError("Isolated execution requires a default timeout")
        self.queue = queue
        self.lane = JobLane(lane)
        self.registry = registry
        self.on_finished = on_finished
        self.runner = runner
        self.timeout_seconds = timeout_seconds
        self.job_timeouts = timeouts
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self.last_model_key: str | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        if isinstance(self.registry, HandlerRegistry):
            self.registry.register(job_type, handler)
        else:
            self.registry[job_type] = handler

    def run_once(self, stop_event: threading.Event | None = None) -> bool:
        if stop_event is not None and stop_event.is_set():
            return False
        self.queue.recover_stale()
        return self._run_once(stop_event)

    def _run_once(self, stop_event: threading.Event | None = None) -> bool:
        if stop_event is not None and stop_event.is_set():
            return False
        job = self.queue.claim_next(self.lane, self.last_model_key)
        if job is None:
            return False
        with log_context(project_id=job.project_id, job_id=job.id):
            return self._execute(job, stop_event)

    def _execute(self, job: Job, stop_event: threading.Event | None) -> bool:
        claim_token = job.claim_token
        assert claim_token is not None
        self.last_model_key = job.model_key
        _LOGGER.info(
            "Started job %s",
            job.id,
            extra={
                "event": "job_started",
                "lane": self.lane.value,
                "job_type": job.job_type,
                "attempt": job.attempts,
            },
        )
        handler = self._get_handler(job.job_type)
        if handler is None:
            try:
                finished = self.queue.fail(
                    job.id,
                    f"Unknown job_type: {job.job_type}",
                    retry=False,
                    claim_token=claim_token,
                )
            except ClaimLostError:
                _LOGGER.warning("Lost ownership of job %s before failing it", job.id)
            else:
                self._notify(finished)
                self._log_finished(finished)
            return True
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, claim_token, stop_heartbeat, job.project_id),
            name=f"job-lease-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            try:
                if self.runner is None:
                    result = handler(job)
                else:
                    assert self.timeout_seconds is not None
                    result = self.runner.run(
                        handler,
                        job,
                        timeout=self.job_timeouts.get(job.job_type, self.timeout_seconds),
                        stop_event=stop_event,
                        shutdown_grace=self.shutdown_grace_seconds,
                    )
            except ProcessCleanupError as exc:
                # Do not let heartbeat overwrite the permanent quarantine lease.
                stop_heartbeat.set()
                heartbeat.join()
                self.queue.quarantine(job.id, str(exc), claim_token=claim_token)
                _LOGGER.critical(
                    "Job process cleanup failed; lane held for verified manual recovery",
                    extra={"event": "job_quarantined", "lane": self.lane.value},
                    exc_info=True,
                )
                raise
            except Exception:
                finished = self.queue.fail(job.id, traceback.format_exc(), claim_token=claim_token)
            else:
                finished = self.queue.complete(job.id, result, claim_token=claim_token)
        except ClaimLostError:
            _LOGGER.warning("Lost ownership of job %s; discarding its late result", job.id)
            return True
        finally:
            stop_heartbeat.set()
            heartbeat.join()
        self._notify(finished)
        self._log_finished(finished)
        return True

    def _log_finished(self, job: Job) -> None:
        _LOGGER.info(
            "Job %s is %s",
            job.id,
            job.status.value,
            extra={
                "event": "job_finished",
                "lane": self.lane.value,
                "job_type": job.job_type,
                "attempt": job.attempts,
                "status": job.status.value,
            },
        )

    def _heartbeat(
        self,
        job_id: str,
        claim_token: str,
        stop_event: threading.Event,
        project_id: str | None = None,
    ) -> None:
        while not stop_event.wait(self.queue.lease_seconds / 3):
            try:
                if not self.queue.heartbeat(job_id, claim_token):
                    return
            except Exception:
                # A transient write lock must not silently terminate renewal.
                with log_context(project_id=project_id, job_id=job_id):
                    _LOGGER.exception("Could not renew lease for job %s", job_id)

    def run_forever(
        self,
        stop_event: threading.Event,
        poll_interval: float = 0.25,
    ) -> None:
        while not stop_event.is_set():
            did_work = self.run_once(stop_event)
            if not did_work:
                stop_event.wait(max(0.0, poll_interval))

    def _get_handler(self, job_type: str) -> JobHandler | None:
        return self.registry.get(job_type)

    def _notify(self, job: Job) -> None:
        if self.on_finished is not None:
            try:
                self.on_finished(job)
            except Exception:
                _LOGGER.exception("on_finished callback failed for job %s", job.id)


__all__ = ["HandlerRegistry", "JobHandler", "Worker"]
