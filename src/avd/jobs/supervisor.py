"""Own the worker processes started by this local web server.

Queue activity and managed processes are deliberately reported separately: an
independently launched worker may be executing a job while our process is idle.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from sqlalchemy import update

from avd.config import Settings
from avd.observability import configure_logging

from .models import Job, JobLane, JobStatus
from .queue import JobQueue
from .worker import Worker

_LOGGER = logging.getLogger(__name__)


class StopSignal(Protocol):
    def set(self) -> None: ...

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class WorkerProcess(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def exitcode(self) -> int | None: ...

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def join(self, timeout: float | None = None) -> None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def close(self) -> None: ...


@dataclass
class ActiveClaim:
    """One small shared snapshot, written before executing a claimed job.

    No interprocess mutex is used: a worker killed while holding a mutex could
    block shutdown forever. A partial write cannot authorize another attempt;
    fencing requires an exact database job ID and claim token match. The parent
    reads once the worker has had its grace period, and again after termination.
    """

    storage: Any

    @classmethod
    def create(cls) -> ActiveClaim:
        return cls(multiprocessing.get_context("spawn").RawArray("B", 512))

    def set(self, job: Job) -> None:
        encoded = json.dumps([job.id, job.claim_token]).encode("ascii")
        if len(encoded) >= len(self.storage):
            raise ValueError("Worker claim exceeds shared snapshot capacity")
        self.storage[:] = encoded + b"\0" * (len(self.storage) - len(encoded))

    def get(self) -> tuple[str, str] | None:
        try:
            value = json.loads(bytes(self.storage).split(b"\0", 1)[0])
        except (ValueError, UnicodeDecodeError):
            return None
        if (
            not isinstance(value, list)
            or len(value) != 2
            or not all(isinstance(item, str) and item for item in value)
        ):
            return None
        return value[0], value[1]


class _ReportingQueue(JobQueue):
    def __init__(self, queue: JobQueue, active_claim: ActiveClaim) -> None:
        super().__init__(queue.session_factory, lease_seconds=queue.lease_seconds)
        self.active_claim = active_claim

    def claim_next(self, lane: JobLane | str, current_model_key: str | None = None) -> Job | None:
        job = super().claim_next(lane, current_model_key)
        if job is not None:
            # The handler cannot run before its exact ownership is published.
            self.active_claim.set(job)
        return job


class WorkerRuntime(Protocol):
    queue: JobQueue

    def build_worker(self, lane: JobLane | str) -> Worker: ...

    def close(self) -> None: ...


def _load_settings(base: Settings) -> Settings:
    from avd.settings_store import SettingsStore

    return SettingsStore(base).load()


def run_worker_loop(
    base_settings: Settings,
    lane: JobLane,
    stop: StopSignal,
    active_claim: ActiveClaim,
    *,
    runtime_factory: Callable[[Settings], WorkerRuntime] | None = None,
    settings_loader: Callable[[Settings], Settings] = _load_settings,
    poll_interval: float = 0.25,
) -> None:
    """Reload local configuration between attempts, never during an attempt."""
    from avd.runtime import Runtime

    factory = runtime_factory or Runtime
    last_model_key = None
    runtime = None
    worker = None
    loaded_settings = None
    try:
        while not stop.is_set():
            settings = settings_loader(base_settings)
            if worker is None or settings != loaded_settings:
                if runtime is not None:
                    runtime.close()
                    runtime = None
                configure_logging(settings.log_format, settings.log_level)
                runtime = factory(settings)
                worker = runtime.build_worker(lane)
                worker.queue = _ReportingQueue(runtime.queue, active_claim)
                worker.last_model_key = last_model_key
                loaded_settings = settings
            did_work = worker.run_once(cast(threading.Event, stop))
            last_model_key = worker.last_model_key
            if not did_work:
                stop.wait(poll_interval)
    finally:
        if runtime is not None:
            runtime.close()


def _child_main(
    settings: Settings, lane: JobLane, stop: StopSignal, active_claim: ActiveClaim
) -> None:
    # The server owns Ctrl-C; each child receives a shared graceful stop event.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    parent = multiprocessing.parent_process()
    watchdog_stop = threading.Event()

    def watch_parent() -> None:
        while not watchdog_stop.wait(0.25):
            if parent is not None and not parent.is_alive():
                stop.set()
                return

    watchdog = threading.Thread(target=watch_parent, name="worker-parent", daemon=True)
    watchdog.start()
    try:
        run_worker_loop(settings, lane, stop, active_claim)
    finally:
        watchdog_stop.set()
        watchdog.join(timeout=1)


def _process_factory(
    settings: Settings, lane: JobLane, stop: StopSignal, active_claim: ActiveClaim
) -> WorkerProcess:
    return multiprocessing.get_context("spawn").Process(
        target=_child_main,
        args=(settings, lane, stop, active_claim),
        name=f"avd-worker-{lane.value}",
        daemon=False,
    )


ProcessFactory = Callable[[Settings, JobLane, StopSignal, ActiveClaim], WorkerProcess]


@dataclass
class _ManagedWorker:
    process: WorkerProcess
    stop: StopSignal
    active_claim: ActiveClaim
    pid: int | None = None
    exit_code: int | None = None
    state: str = "starting"
    error: str | None = None
    closed: bool = False
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WorkerSupervisor:
    """Start and stop only this server's children, with bounded shutdown.

    A forced or unexplained worker exit fences its unfinished attempt into the
    queue's existing permanent quarantine. This is conservative: it never frees
    the GPU lane while descendant termination is uncertain.
    """

    def __init__(
        self,
        settings: Settings,
        queue: JobQueue,
        *,
        process_factory: ProcessFactory = _process_factory,
        cleanup_seconds: float = 12.0,
    ) -> None:
        if cleanup_seconds < 0:
            raise ValueError("Cleanup grace must not be negative")
        self.settings = settings
        self.queue = queue
        self.process_factory = process_factory
        self.cleanup_seconds = cleanup_seconds
        self._workers: dict[JobLane, _ManagedWorker] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _lanes(lane: JobLane | str) -> list[JobLane]:
        return list(JobLane) if lane == "all" else [JobLane(lane)]

    def _fence(self, managed: _ManagedWorker, reason: str) -> None:
        claim = managed.active_claim.get()
        if claim is None:
            return
        job_id, token = claim
        with self.queue.session_factory() as session:
            # Rotate the token as well as extending the lease: a stuck worker
            # must not overwrite the quarantine with its heartbeat or result.
            session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.RUNNING,
                    Job.claim_token == token,
                )
                .values(
                    claim_token=str(uuid.uuid4()),
                    lease_expires_at=datetime.max.replace(tzinfo=timezone.utc),
                    error=f"Managed worker cleanup uncertain; manual recovery required: {reason}",
                )
            )
            session.commit()

    def _refresh(self, managed: _ManagedWorker) -> None:
        if managed.closed or managed.process.is_alive():
            return
        managed.process.join(timeout=0)
        managed.exit_code = managed.process.exitcode
        self._fence(managed, "worker exited with an unfinished attempt")
        if managed.state not in {"stopped", "failed"}:
            managed.state = "stopped" if managed.exit_code == 0 else "failed"
        if managed.exit_code not in {0, None} and managed.error is None:
            managed.error = "Worker process exited unexpectedly"
        managed.process.close()
        managed.closed = True

    def start(self, lane: JobLane | str = "all") -> dict[str, Any]:
        lanes = self._lanes(lane)
        with self._lock:
            if self._closed:
                raise RuntimeError("Worker supervisor has shut down")
            for selected in lanes:
                existing = self._workers.get(selected)
                if existing is not None:
                    self._refresh(existing)
                    if not existing.closed:
                        continue
                stop = multiprocessing.get_context("spawn").Event()
                active = ActiveClaim.create()
                process = self.process_factory(self.settings, selected, stop, active)
                managed = _ManagedWorker(process, stop, active)
                self._workers[selected] = managed
                try:
                    process.start()
                    managed.pid = process.pid
                    managed.state = "running"
                except Exception:
                    managed.error = "Worker process could not be started"
                    managed.state = "failed"
                    try:
                        if process.is_alive():
                            stop.set()
                            process.terminate()
                            process.join(timeout=1)
                        if not process.is_alive():
                            process.close()
                            managed.closed = True
                    except Exception:
                        _LOGGER.exception("Could not clean up a worker that failed to start")
                    _LOGGER.exception("Could not start %s worker", selected.value)
            return self.status()

    def stop(self, lane: JobLane | str = "all") -> dict[str, Any]:
        lanes = self._lanes(lane)
        with self._lock:
            selected = [self._workers[item] for item in lanes if item in self._workers]
            for managed in selected:
                self._refresh(managed)
                if not managed.closed:
                    managed.stop.set()
                    managed.state = "stopping"
            try:
                grace = _load_settings(self.settings).worker_shutdown_grace_seconds
            except Exception:
                # Invalid edits on disk must not prevent stopping owned workers.
                _LOGGER.error("Could not reload shutdown settings; using startup grace")
                grace = self.settings.worker_shutdown_grace_seconds
            deadline = time.monotonic() + grace + self.cleanup_seconds
            for managed in selected:
                if managed.closed:
                    continue
                managed.process.join(timeout=max(0.0, deadline - time.monotonic()))
            forced = [
                managed for managed in selected if not managed.closed and managed.process.is_alive()
            ]
            for managed in forced:
                managed.error = "Graceful stop exceeded its deadline; active attempt quarantined"
                self._fence(managed, "graceful shutdown deadline exceeded")
                try:
                    managed.process.terminate()
                except OSError:
                    _LOGGER.exception("Worker termination failed; trying forced termination")
            terminate_deadline = time.monotonic() + 1.0
            for managed in forced:
                managed.process.join(timeout=max(0.0, terminate_deadline - time.monotonic()))
                if managed.process.is_alive():
                    try:
                        managed.process.kill()
                    except OSError:
                        _LOGGER.exception("Worker forced termination failed")
            kill_deadline = time.monotonic() + 1.0
            for managed in forced:
                managed.process.join(timeout=max(0.0, kill_deadline - time.monotonic()))
                self._fence(managed, "forced worker stop")
                managed.state = "failed"
                if managed.process.is_alive():
                    managed.error = "Worker termination unconfirmed; active attempt quarantined"
            for managed in selected:
                self._refresh(managed)
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            for existing in self._workers.values():
                self._refresh(existing)
            running = self.queue.list(status=JobStatus.RUNNING)
            result = []
            for lane in JobLane:
                managed = self._workers.get(lane)
                jobs = [job for job in running if job.lane == lane]
                claim = managed.active_claim.get() if managed is not None else None
                active_job = next(
                    (
                        job.id
                        for job in jobs
                        if claim is not None and (job.id, job.claim_token) == claim
                    ),
                    None,
                )
                result.append(
                    {
                        "lane": lane.value,
                        "state": managed.state if managed is not None else "stopped",
                        "managed": managed is not None,
                        "pid": managed.pid if managed is not None else None,
                        "exit_code": managed.exit_code if managed is not None else None,
                        "error": managed.error if managed is not None else None,
                        "started_at": managed.started_at.isoformat() if managed else None,
                        "active_job_id": active_job,
                        "running_job_ids": [job.id for job in jobs],
                        "quarantined_job_ids": [
                            job.id
                            for job in jobs
                            if job.lease_expires_at is not None
                            and job.lease_expires_at.year == datetime.max.year
                        ],
                    }
                )
            return {"workers": result}

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.stop()
                self._closed = True


__all__ = ["ActiveClaim", "WorkerSupervisor", "run_worker_loop"]
