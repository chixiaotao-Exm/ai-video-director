from __future__ import annotations

from threading import Lock, Thread
import time

import pytest

from avd.jobs import HandlerRegistry, JobLane, JobQueue, JobStatus, Worker
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

    assert queue.recover_stale() == 1
    recovered = queue.get(job_id)
    assert recovered.status == JobStatus.QUEUED
    assert recovered.started_at is None


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
