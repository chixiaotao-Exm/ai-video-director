from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from avd.config import Settings
from avd.jobs import Job, JobQueue, JobStatus, Worker
from avd.jobs import process as process_module
from avd.jobs.process import ProcessCleanupError, ProcessRunner
from avd.llm import FakeProvider
from avd.runtime import Runtime
from avd.state.db import create_engine, create_session_factory, init_db


def offline_handler(job: Job) -> dict[str, Any]:
    # Spawn does not inherit pytest monkeypatches. These pure fake handlers must
    # independently forbid sockets if future edits accidentally add network work.
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Spawned test handlers must remain offline")

    socket.socket.connect = blocked
    socket.create_connection = blocked
    if job.payload.get("crash"):
        os._exit(7)
    if job.payload.get("raise"):
        raise ValueError("fake child failure")
    if job.payload.get("descendant"):
        code = (
            "import pathlib,time; p=pathlib.Path(__import__('sys').argv[1]); "
            "\nwhile True:\n p.write_text(str(time.monotonic())); time.sleep(.03)"
        )
        subprocess.Popen(
            [sys.executable, "-c", code, job.payload["descendant"]],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    if job.payload.get("started"):
        Path(job.payload["started"]).write_text("started")
    time.sleep(job.payload.get("delay", 0))
    if job.payload.get("late"):
        Path(job.payload["late"]).write_text("late side effect")
    return {"pid": os.getpid(), "attempt": job.attempts}


@pytest.fixture
def persistent_queue(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    init_db(engine)
    try:
        yield JobQueue(create_session_factory(engine), lease_seconds=0.6)
    finally:
        engine.dispose()


def process_worker(queue: JobQueue, **kwargs: Any) -> Worker:
    return Worker(
        queue,
        "gpu",
        {"work": offline_handler},
        runner=ProcessRunner(),
        timeout_seconds=10,
        **kwargs,
    )


def test_success_uses_separate_process_and_notifies(persistent_queue: JobQueue):
    job_id = persistent_queue.submit("gpu", "work", {})
    notifications = []
    worker = process_worker(persistent_queue, on_finished=notifications.append)
    assert worker.run_once()
    job = persistent_queue.get(job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.result["pid"] != os.getpid()
    assert notifications[0].id == job_id
    assert job.claim_token is None


@pytest.mark.parametrize(
    "payload, text",
    [
        ({"crash": True}, "without returning a result"),
        ({"raise": True}, "fake child failure"),
    ],
)
def test_child_failure_uses_normal_retry_policy(persistent_queue: JobQueue, payload, text):
    job_id = persistent_queue.submit("gpu", "work", payload, max_attempts=2)
    worker = process_worker(persistent_queue)
    assert worker.run_once()
    assert persistent_queue.get(job_id).status == JobStatus.QUEUED
    assert worker.run_once()
    job = persistent_queue.get(job_id)
    assert job.status == JobStatus.FAILED and job.attempts == 2
    assert text in job.error


def test_timeout_override_retries_and_prevents_late_side_effects(
    persistent_queue: JobQueue, tmp_path: Path
):
    late = tmp_path / "late.txt"
    job_id = persistent_queue.submit(
        "gpu", "work", {"delay": 20, "late": str(late)}, max_attempts=2
    )
    worker = process_worker(persistent_queue, job_timeouts={"work": 1.5})
    started = time.monotonic()
    assert worker.run_once()
    assert persistent_queue.get(job_id).status == JobStatus.QUEUED
    assert worker.run_once()
    job = persistent_queue.get(job_id)
    assert job.status == JobStatus.FAILED and job.attempts == 2
    assert "timed out after 1.5s" in job.error
    assert time.monotonic() - started < 12
    assert not late.exists()


def test_timeout_stops_descendants_before_releasing_gpu(persistent_queue: JobQueue, tmp_path: Path):
    ticks = tmp_path / "descendant.txt"
    started_file = tmp_path / "started.txt"
    job_id = persistent_queue.submit(
        "gpu",
        "work",
        {"delay": 20, "descendant": str(ticks), "started": str(started_file)},
        max_attempts=1,
    )
    next_id = persistent_queue.submit("gpu", "work", {})
    worker = process_worker(persistent_queue, job_timeouts={"work": 3})
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.run_once)
        wait_for_file(ticks)
        # Lease renewal continues while an isolated child occupies the GPU lane.
        time.sleep(0.8)
        assert persistent_queue.recover_stale() == 0
        assert persistent_queue.claim_next("gpu") is None
        assert pending.result(timeout=10)
    assert persistent_queue.get(job_id).status == JobStatus.FAILED
    snapshot = ticks.read_text()
    time.sleep(0.2)
    assert ticks.read_text() == snapshot
    assert persistent_queue.claim_next("gpu").id == next_id


def wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 8
    while not path.exists():
        assert time.monotonic() < deadline, f"Child did not create {path.name}"
        time.sleep(0.025)


@pytest.mark.parametrize(
    "delay, grace, expected",
    [
        (0.2, 2.0, JobStatus.SUCCEEDED),
        (20.0, 0.15, JobStatus.QUEUED),
    ],
)
def test_shutdown_drains_or_stops_current_attempt_without_claiming_next(
    persistent_queue: JobQueue, tmp_path: Path, delay, grace, expected
):
    started_file = tmp_path / "started.txt"
    first_id = persistent_queue.submit(
        "gpu", "work", {"started": str(started_file), "delay": delay}, max_attempts=2
    )
    next_id = persistent_queue.submit("gpu", "work", {})
    stop = Event()
    worker = process_worker(persistent_queue, shutdown_grace_seconds=grace)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.run_forever, stop)
        wait_for_file(started_file)
        stop.set()
        pending.result(timeout=10)
    job = persistent_queue.get(first_id)
    assert job.status == expected
    if expected == JobStatus.QUEUED:
        assert "shutdown grace" in job.error
    assert persistent_queue.get(next_id).attempts == 0


def test_stopped_worker_does_not_claim(persistent_queue: JobQueue):
    job_id = persistent_queue.submit("gpu", "work", {})
    stop = Event()
    stop.set()
    assert process_worker(persistent_queue).run_once(stop) is False
    assert persistent_queue.get(job_id).attempts == 0


def test_cleanup_failure_quarantines_lane_until_exclusive_recovery(persistent_queue: JobQueue):
    class UncertainRunner:
        def run(self, *args, **kwargs):
            raise ProcessCleanupError("simulated unavailable termination facility")

    job_id = persistent_queue.submit("gpu", "work", {}, max_attempts=1)
    persistent_queue.submit("gpu", "work", {})
    worker = Worker(
        persistent_queue,
        "gpu",
        {"work": offline_handler},
        runner=UncertainRunner(),
        timeout_seconds=1,
    )
    with pytest.raises(ProcessCleanupError):
        worker.run_once()
    job = persistent_queue.get(job_id)
    assert job.status == JobStatus.RUNNING
    assert "manual recovery required" in job.error
    assert persistent_queue.recover_stale() == 0
    assert persistent_queue.claim_next("gpu") is None
    assert persistent_queue.recover_stale(force=True) == 1
    assert persistent_queue.get(job_id).status == JobStatus.FAILED


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_worker_rejects_invalid_deadlines(persistent_queue: JobQueue, value):
    with pytest.raises(ValueError, match="positive finite"):
        process_worker(persistent_queue, job_timeouts={"work": value})


def test_timeout_requires_isolation(persistent_queue: JobQueue):
    with pytest.raises(ValueError, match="isolated"):
        Worker(persistent_queue, "gpu", {}, timeout_seconds=1)


def test_runtime_worker_reconstructs_runtime_and_fails_before_provider_call(tmp_path: Path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    try:
        # No project exists: child preflight rejects before accessing a service.
        job_id = runtime.queue.submit("api", "image_gen", {}, max_attempts=1)
        assert runtime.build_worker("api").run_once()
        job = runtime.queue.get(job_id)
        assert job.status == JobStatus.FAILED
        assert "no project node" in job.error
    finally:
        runtime.close()


def test_runtime_worker_rejects_silently_discarding_fake_providers(tmp_path: Path):
    runtime = Runtime(
        Settings(_env_file=None, data_dir=tmp_path), providers={"x": FakeProvider([])}
    )
    try:
        with pytest.raises(ValueError, match="injected providers"):
            runtime.build_worker("api")
    finally:
        runtime.close()


@pytest.mark.parametrize("state, active", [("S", True), ("R", True), ("Z", False), ("X", False)])
def test_posix_cleanup_distinguishes_live_descendants_from_zombies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, active: bool
):
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: None, raising=False)
    monkeypatch.setattr(process_module, "Path", lambda path: tmp_path)
    entry = tmp_path / "123"
    entry.mkdir()
    (entry / "stat").write_text(f"123 (worker with (parens)) {state} 1 123 123 0 0")
    assert process_module._posix_group_active(123) is active


def test_posix_cleanup_treats_unreadable_state_as_unsafe(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(process_module.os, "killpg", lambda pid, sig: None, raising=False)
    monkeypatch.setattr(process_module, "Path", lambda path: tmp_path)
    entry = tmp_path / "123"
    entry.mkdir()
    (entry / "stat").write_text("malformed kernel process data")
    assert process_module._posix_group_active(123) is True


@pytest.mark.parametrize("observed_parent, expected", [(456, []), (1, [(123, 9)])])
def test_posix_parent_watcher_kills_only_an_orphan_group(monkeypatch, observed_parent, expected):
    class ScriptedStop:
        def __init__(self):
            self.waits = iter([False, True])

        def wait(self, interval):
            assert interval == 0.1
            return next(self.waits)

    signals = []
    monkeypatch.setattr(process_module.os, "getppid", lambda: observed_parent)
    monkeypatch.setattr(process_module.os, "getpid", lambda: 123)
    monkeypatch.setattr(process_module.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(
        process_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)), raising=False
    )
    process_module._watch_parent(456, ScriptedStop())
    assert signals == expected


def test_posix_parent_watcher_stops_with_handler(monkeypatch):
    stop = Event()
    stop.set()

    def unexpected():
        pytest.fail("A stopped parent watcher must not query or signal processes")

    monkeypatch.setattr(process_module.os, "getppid", unexpected)
    process_module._watch_parent(456, stop)
