"""Background workers for executing queued jobs."""

from __future__ import annotations

import logging
import threading
import traceback
from collections.abc import Callable, MutableMapping
from typing import Any

from .models import Job, JobLane
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
    ) -> None:
        self.queue = queue
        self.lane = JobLane(lane)
        self.registry = registry
        self.on_finished = on_finished
        self.last_model_key: str | None = None

    def register(self, job_type: str, handler: JobHandler) -> None:
        if isinstance(self.registry, HandlerRegistry):
            self.registry.register(job_type, handler)
        else:
            self.registry[job_type] = handler

    def run_once(self) -> bool:
        self.queue.recover_stale()
        return self._run_once()

    def _run_once(self) -> bool:
        job = self.queue.claim_next(self.lane, self.last_model_key)
        if job is None:
            return False
        claim_token = job.claim_token
        assert claim_token is not None
        self.last_model_key = job.model_key
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
            return True
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat,
            args=(job.id, claim_token, stop_heartbeat),
            name=f"job-lease-{job.id}",
            daemon=True,
        )
        heartbeat.start()
        try:
            try:
                result = handler(job)
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
        return True

    def _heartbeat(self, job_id: str, claim_token: str, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.queue.lease_seconds / 3):
            try:
                if not self.queue.heartbeat(job_id, claim_token):
                    return
            except Exception:
                # A transient write lock must not silently terminate renewal.
                _LOGGER.exception("Could not renew lease for job %s", job_id)

    def run_forever(
        self,
        stop_event: threading.Event,
        poll_interval: float = 0.25,
    ) -> None:
        while not stop_event.is_set():
            did_work = self.run_once()
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
