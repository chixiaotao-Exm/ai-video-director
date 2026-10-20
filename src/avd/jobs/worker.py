"""Background workers for executing queued jobs."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
import threading
import time
import traceback
from typing import Any

from .models import Job, JobLane
from .queue import JobQueue


JobHandler = Callable[[Job], dict[str, Any]]
_GPU_RUN_LOCK = threading.Lock()


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
    ):
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
        if self.lane == JobLane.GPU:
            with _GPU_RUN_LOCK:
                return self._run_once()
        return self._run_once()

    def _run_once(self) -> bool:
        job = self.queue.claim_next(self.lane, self.last_model_key)
        if job is None:
            return False
        self.last_model_key = job.model_key
        handler = self._get_handler(job.job_type)
        if handler is None:
            finished = self.queue.fail(
                job.id,
                f"Unknown job_type: {job.job_type}",
                retry=False,
            )
            self._notify(finished)
            return True
        try:
            result = handler(job)
        except Exception:
            finished = self.queue.fail(job.id, traceback.format_exc())
        else:
            finished = self.queue.complete(job.id, result)
        self._notify(finished)
        return True

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
            self.on_finished(job)


__all__ = ["HandlerRegistry", "JobHandler", "Worker"]
