from __future__ import annotations

import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

import pytest
from sqlalchemy.orm import sessionmaker

from avd.jobs import ClaimLostError, HandlerRegistry, Job, JobQueue, JobStatus, Worker
from avd.state.db import create_engine, create_session_factory, init_db


@pytest.fixture
def queue(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return JobQueue(create_session_factory(engine))


def test_submit_claim_and_complete(queue):
    job_id = queue.submit("cpu", "ffmpeg", {"input": "a.mp4"})

    claimed = queue.claim_next("cpu")
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.payload == {"input": "a.mp4"}

    completed = queue.complete(job_id, {"output": "b.mp4"})
    assert completed.status == JobStatus.SUCCEEDED
    assert queue.get(job_id).result == {"output": "b.mp4"}


def test_priority_order(queue):
    low = queue.submit("cpu", "low", {}, priority=0)
    high = queue.submit("cpu", "high", {}, priority=10)

    assert queue.claim_next("cpu").id == high
    queue.complete(high, {})
    assert queue.claim_next("cpu").id == low


def test_gpu_model_key_grouping(queue):
    first_a = queue.submit("gpu", "image_to_video", {}, model_key="A")
    first_b = queue.submit("gpu", "image_to_video", {}, model_key="B")
    second_a = queue.submit("gpu", "image_to_video", {}, model_key="A")

    one = queue.claim_next("gpu")
    assert one.id == first_a
    queue.complete(one.id, {})
    two = queue.claim_next("gpu", current_model_key="A")
    assert two.id == second_a
    queue.complete(two.id, {})
    three = queue.claim_next("gpu", current_model_key="A")
    assert three.id == first_b


def test_fail_retries_until_max_attempts(queue):
    job_id = queue.submit("api", "remote", {}, max_attempts=3)

    for attempt in (1, 2):
        claimed = queue.claim_next("api")
        assert claimed.attempts == attempt
        failed = queue.fail(job_id, f"failure {attempt}")
        assert failed.status == JobStatus.QUEUED
    claimed = queue.claim_next("api")
    assert claimed.attempts == 3
    failed = queue.fail(job_id, "final failure")
    assert failed.status == JobStatus.FAILED
    assert queue.claim_next("api") is None


def test_cancel_only_queued_jobs(queue):
    queued_id = queue.submit("cpu", "one", {})
    running_id = queue.submit("cpu", "two", {})

    assert queue.cancel(queued_id) is True
    assert queue.cancel(queued_id) is False
    running = queue.claim_next("cpu")
    assert running.id == running_id
    assert queue.cancel(running_id) is False
    assert queue.get(queued_id).status == JobStatus.CANCELLED
    assert queue.get(running_id).status == JobStatus.RUNNING


def test_recover_stale_jobs(queue):
    job_id = queue.submit("gpu", "lipsync", {})
    assert queue.claim_next("gpu").status == JobStatus.RUNNING
    _expire_lease(queue, job_id)

    assert queue.recover_stale() == 1
    recovered = queue.get(job_id)
    assert recovered.status == JobStatus.QUEUED
    assert recovered.started_at is None


def test_recover_stale_exhausted_job_fails_as_worker_crash(queue):
    job_id = queue.submit("gpu", "lipsync", {}, max_attempts=1)
    claimed = queue.claim_next("gpu")
    assert claimed is not None
    assert claimed.attempts == claimed.max_attempts
    _expire_lease(queue, job_id)

    assert queue.recover_stale() == 1

    recovered = queue.get(job_id)
    assert recovered.status == JobStatus.FAILED
    assert recovered.error == "worker crashed"
    assert recovered.started_at is None
    assert recovered.finished_at is not None


def test_worker_unknown_job_type_fails_without_retry(queue):
    registry = HandlerRegistry()
    worker = Worker(queue, "api", registry)
    job_id = queue.submit("api", "not_registered", {}, max_attempts=3)

    assert worker.run_once() is True
    job = queue.get(job_id)
    assert job.status == JobStatus.FAILED
    assert job.attempts == 1
    assert queue.claim_next("api") is None


def test_worker_handler_exception_retries_and_reports_traceback(queue):
    registry = HandlerRegistry()
    registry.register("flaky", lambda job: 1 / 0)
    worker = Worker(queue, "cpu", registry)
    job_id = queue.submit("cpu", "flaky", {}, max_attempts=1)

    assert worker.run_once() is True
    job = queue.get(job_id)
    assert job.status == JobStatus.FAILED
    assert "ZeroDivisionError" in job.error


def test_worker_on_finished_exception_is_logged_and_run_forever_continues(queue, caplog):
    registry = HandlerRegistry()
    processed: list[str] = []
    registry.register("work", lambda job: processed.append(job.id) or {"ok": True})
    stop_event = Event()
    notifications: list[str] = []

    def on_finished(job):
        notifications.append(job.id)
        if len(notifications) == 1:
            raise RuntimeError("notification failed")
        stop_event.set()

    worker = Worker(queue, "cpu", registry, on_finished=on_finished)
    first_id = queue.submit("cpu", "work", {})
    second_id = queue.submit("cpu", "work", {})

    with caplog.at_level("ERROR", logger="avd.jobs.worker"):
        worker.run_forever(stop_event, poll_interval=0.001)

    assert processed == [first_id, second_id]
    assert notifications == [first_id, second_id]
    assert queue.get(first_id).status == JobStatus.SUCCEEDED
    assert queue.get(second_id).status == JobStatus.SUCCEEDED
    assert "on_finished callback failed" in caplog.text


def test_two_api_workers_do_not_claim_same_job(tmp_path):
    db_path = tmp_path / "jobs.db"
    engine = create_engine(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = create_session_factory(engine)
    queue = JobQueue(session_factory)
    job_ids = [queue.submit("api", "work", {"n": i}) for i in range(20)]
    claimed_ids: list[str] = []
    lock = Lock()
    registry = HandlerRegistry()

    def handler(job):
        with lock:
            claimed_ids.append(job.id)
        time.sleep(0.005)
        return {"ok": True}

    registry.register("work", handler)
    workers = [Worker(queue, "api", registry), Worker(queue, "api", registry)]

    def run(worker):
        for _ in range(20):
            worker.run_once()

    threads = [Thread(target=run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sorted(claimed_ids) == sorted(job_ids)
    assert len(claimed_ids) == len(set(claimed_ids))
    assert all(job.status == JobStatus.SUCCEEDED for job in queue.list())


def _expire_lease(queue, job_id):
    with queue.session_factory() as session:
        job = session.get(Job, job_id)
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()


def _claim_gpu_in_process(database_url):
    engine = create_engine(database_url)
    try:
        queue = JobQueue(sessionmaker(engine))
        job = queue.claim_next("gpu")
        return job.id if job is not None else None
    finally:
        engine.dispose()


def test_gpu_claim_is_serial_across_processes(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'gpu-processes.db'}"
    engine = create_engine(database_url)
    init_db(engine)
    queue = JobQueue(sessionmaker(engine))
    job_ids = {queue.submit("gpu", "work", {}) for _ in range(4)}

    with ProcessPoolExecutor(
        max_workers=2, mp_context=multiprocessing.get_context("spawn")
    ) as pool:
        claims = list(pool.map(_claim_gpu_in_process, [database_url] * 4))

    successful = [job_id for job_id in claims if job_id is not None]
    assert len(successful) == 1
    assert successful[0] in job_ids
    queue.complete(successful[0], {})
    assert queue.claim_next("gpu") is not None
    engine.dispose()


def test_gpu_occupancy_does_not_block_other_lanes(queue):
    queue.submit("gpu", "one", {})
    queue.submit("gpu", "two", {})
    queue.submit("api", "remote", {})
    assert queue.claim_next("gpu") is not None
    assert queue.claim_next("gpu") is None
    assert queue.claim_next("api") is not None


def test_queue_returns_loaded_jobs_with_default_expiring_sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'expiring.db'}")
    init_db(engine)
    queue = JobQueue(sessionmaker(engine))
    job_id = queue.submit("cpu", "work", {"value": 1}, max_attempts=1)

    claimed = queue.claim_next("cpu")
    assert claimed.id == job_id
    assert claimed.payload == {"value": 1}
    assert claimed.claim_token is not None
    assert claimed.lease_expires_at is not None
    failed = queue.fail(job_id, "failed", claim_token=claimed.claim_token)
    assert failed.status == JobStatus.FAILED
    retried = queue.retry(job_id)
    assert retried.status == JobStatus.QUEUED
    assert retried.attempts == 1
    claimed = queue.claim_next("cpu")
    completed = queue.complete(job_id, {"done": True}, claim_token=claimed.claim_token)
    assert completed.status == JobStatus.SUCCEEDED
    assert completed.result == {"done": True}
    assert completed.claim_token is None
    assert queue.get(job_id).payload == {"value": 1}
    assert queue.list()[0].status == JobStatus.SUCCEEDED
    engine.dispose()


def test_recovery_preserves_active_leases_and_force_is_explicit(queue):
    job_id = queue.submit("cpu", "work", {})
    claimed = queue.claim_next("cpu")
    assert queue.recover_stale() == 0
    assert queue.get(job_id).claim_token == claimed.claim_token

    assert queue.recover_stale(force=True) == 1
    assert queue.get(job_id).status == JobStatus.QUEUED
    assert queue.get(job_id).claim_token is None


@pytest.mark.parametrize("late_action", ["complete", "fail"])
def test_previous_attempt_cannot_overwrite_new_claim(queue, late_action):
    job_id = queue.submit("cpu", "work", {})
    previous = queue.claim_next("cpu")
    _expire_lease(queue, job_id)
    assert queue.recover_stale() == 1
    current = queue.claim_next("cpu")
    assert current.claim_token != previous.claim_token
    assert queue.heartbeat(job_id, previous.claim_token) is False

    with pytest.raises(ClaimLostError):
        if late_action == "complete":
            queue.complete(job_id, {"late": True}, claim_token=previous.claim_token)
        else:
            queue.fail(job_id, "late failure", claim_token=previous.claim_token)

    unchanged = queue.get(job_id)
    assert unchanged.status == JobStatus.RUNNING
    assert unchanged.claim_token == current.claim_token
    assert unchanged.attempts == 2
    queue.complete(job_id, {"current": True}, claim_token=current.claim_token)


def test_worker_renews_lease_during_long_handler(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'heartbeat.db'}")
    init_db(engine)
    queue = JobQueue(sessionmaker(engine), lease_seconds=0.6)
    observer = JobQueue(sessionmaker(engine), lease_seconds=0.6)
    started = Event()
    release = Event()

    def handler(job):
        started.set()
        assert release.wait(timeout=5)
        return {"ok": True}

    job_id = queue.submit("gpu", "work", {})
    queue.submit("gpu", "work", {})
    worker = Worker(queue, "gpu", {"work": handler})
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(worker.run_once)
        try:
            assert started.wait(timeout=5)
            initial_expiry = observer.get(job_id).lease_expires_at
            time.sleep(1.3)
            assert observer.get(job_id).lease_expires_at > initial_expiry
            assert observer.recover_stale() == 0
            assert observer.claim_next("gpu") is None
        finally:
            release.set()
        assert running.result(timeout=5) is True
    assert queue.get(job_id).status == JobStatus.SUCCEEDED
    engine.dispose()


@pytest.mark.parametrize("raises", [False, True])
def test_worker_discards_late_attempt_without_notifying(tmp_path, raises):
    engine = create_engine(f"sqlite:///{tmp_path / 'late.db'}")
    init_db(engine)
    queue = JobQueue(sessionmaker(engine))
    notifications = []
    job_id = queue.submit("cpu", "work", {})

    def handler(job):
        _expire_lease(queue, job.id)
        assert queue.recover_stale() == 1
        assert queue.claim_next("cpu").attempts == 2
        if raises:
            raise RuntimeError("old attempt failed")
        return {"late": True}

    worker = Worker(queue, "cpu", {"work": handler}, on_finished=notifications.append)
    assert worker.run_once() is True
    assert notifications == []
    assert queue.get(job_id).status == JobStatus.RUNNING
    assert queue.get(job_id).result is None
    assert queue.get(job_id).error is None
    engine.dispose()


@pytest.mark.parametrize("initial_status", ["failed", "cancelled"])
def test_explicit_retry_extends_budget_without_reusing_attempts(queue, initial_status):
    job_id = queue.submit("cpu", "work", {}, max_attempts=1)
    if initial_status == "failed":
        queue.claim_next("cpu")
        queue.fail(job_id, "failure")
    else:
        queue.cancel(job_id)

    prior_attempts = queue.get(job_id).attempts
    retried = queue.retry(job_id, additional_attempts=2)
    assert retried.status == JobStatus.QUEUED
    assert retried.attempts == prior_attempts
    assert retried.max_attempts == prior_attempts + 2
    assert retried.error is None
    assert retried.finished_at is None
    assert retried.claim_token is None
    assert queue.claim_next("cpu").attempts == prior_attempts + 1


def test_explicit_retry_rejects_active_or_succeeded_jobs(queue):
    job_id = queue.submit("cpu", "work", {})
    with pytest.raises(ValueError, match="not failed or cancelled"):
        queue.retry(job_id)
    queue.claim_next("cpu")
    with pytest.raises(ValueError, match="not failed or cancelled"):
        queue.retry(job_id)
    queue.complete(job_id, {})
    with pytest.raises(ValueError, match="not failed or cancelled"):
        queue.retry(job_id)


def test_worker_notifies_retryable_failures(queue):
    job_id = queue.submit("cpu", "work", {}, max_attempts=2)
    notifications = []

    def handler(job):
        raise RuntimeError("retry me")

    worker = Worker(queue, "cpu", {"work": handler}, on_finished=notifications.append)
    assert worker.run_once() is True
    assert [job.status for job in notifications] == [JobStatus.QUEUED]
    assert notifications[0].id == job_id


@pytest.mark.parametrize("lease_seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_lease_duration_rejected(queue, lease_seconds):
    with pytest.raises(ValueError, match="lease_seconds"):
        JobQueue(queue.session_factory, lease_seconds=lease_seconds)
