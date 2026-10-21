from __future__ import annotations

import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Event
from typing import Any

import httpx
import pytest

from avd.jobs import Job, JobQueue, JobStatus, Worker
from avd.jobs.process import ProcessCleanupError, ProcessRunner
from avd.performance.remote import verify_remote_release
from avd.state.db import create_engine, create_session_factory, init_db


def remote_handler(job: Job) -> dict[str, Any]:
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Remote release tests must remain offline")

    socket.socket.connect = blocked
    socket.create_connection = blocked
    journal = Path(job.payload["data_dir"]) / job.payload["output_dir"] / "remote-submission.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(submission(job.job_type)), encoding="utf-8")
    time.sleep(20)
    raise RuntimeError("Offline child should be stopped before this point")


def submission(stage="motion_video", prompt_id="offline-prompt", **updates):
    return {
        "schema_version": 1,
        "stage": stage,
        "state": "submitted" if prompt_id else "submitting",
        "prompt_id": prompt_id,
        "server": "http://127.0.0.1:8188",
        "client_id": "offline-client",
        **updates,
    }


@pytest.fixture
def queue(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    init_db(engine)
    try:
        yield JobQueue(create_session_factory(engine), lease_seconds=0.6)
    finally:
        engine.dispose()


def payload(tmp_path):
    return {
        "data_dir": str(tmp_path),
        "output_dir": "projects/project/node/version",
        "recipe": {"comfyui_profile": {"base_url": "http://127.0.0.1:8188", "timeout": 600}},
    }


def write_journal(tmp_path, record):
    journal = tmp_path / "projects/project/node/version/remote-submission.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(record), encoding="utf-8")
    return journal


def offline_server(*, history=None, running=False, pending=False, queue_response=None):
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET", "Release verification must never mutate ComfyUI"
        assert request.url.host == "127.0.0.1"
        if request.url.path == "/history/offline-prompt":
            return httpx.Response(200, json=history or {})
        if request.url.path == "/queue":
            body = queue_response
            if body is None:
                body = {
                    "queue_running": [[1, "offline-prompt" if running else "another-users-prompt"]],
                    "queue_pending": [[2, "offline-prompt"]] if pending else [],
                }
            return httpx.Response(200, json=body)
        pytest.fail(f"Unexpected offline request {request.url}")

    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize("stage", ["motion_video", "performance_lipsync"])
@pytest.mark.parametrize("location", ["running", "pending"])
def test_active_remote_prompt_quarantines_gpu_until_exclusive_recovery(
    tmp_path, queue, stage, location
):
    write_journal(tmp_path, submission(stage))
    transport, calls = offline_server(**{location: True})
    motion_id = queue.submit("gpu", stage, payload(tmp_path), max_attempts=1)
    seed_id = queue.submit("gpu", "singing_voice", {})

    def failed(job):
        raise TimeoutError("Offline ComfyUI wait timed out")

    worker = Worker(
        queue,
        "gpu",
        {stage: failed},
        failure_guard=partial(verify_remote_release, transport=transport),
    )
    with pytest.raises(ProcessCleanupError, match="still running or queued"):
        worker.run_once()
    stored = queue.get(motion_id)
    assert stored.status == JobStatus.RUNNING
    assert "manual recovery required" in stored.error
    assert "offline-prompt" in stored.error and "remote-submission.json" in stored.error
    assert "--recover-stale --once" in stored.error
    assert queue.get(seed_id).attempts == 0
    restarted = JobQueue(queue.session_factory)
    assert restarted.recover_stale() == 0
    assert restarted.claim_next("gpu") is None
    assert calls == [("GET", "/history/offline-prompt"), ("GET", "/queue")]


@pytest.mark.parametrize("state", ["success", "error", "interrupted", "cancelled"])
def test_terminal_remote_history_allows_normal_failure_and_next_seed(tmp_path, queue, state):
    write_journal(tmp_path, submission())
    transport, calls = offline_server(
        history={"offline-prompt": {"status": {"completed": False, "status_str": state}}}
    )
    motion_id = queue.submit("gpu", "motion_video", payload(tmp_path), max_attempts=1)
    seed_id = queue.submit("gpu", "singing_voice", {})

    def failed(job):
        raise TimeoutError("Offline timed out")

    worker = Worker(
        queue,
        "gpu",
        {"motion_video": failed},
        failure_guard=partial(verify_remote_release, transport=transport),
    )
    assert worker.run_once()
    assert queue.get(motion_id).status == JobStatus.FAILED
    assert "Offline timed out" in queue.get(motion_id).error
    assert queue.claim_next("gpu").id == seed_id
    assert calls == [("GET", "/history/offline-prompt")]


def test_absent_remote_prompt_allows_retry_without_touching_other_work(tmp_path, queue):
    write_journal(tmp_path, submission())
    transport, calls = offline_server()
    motion_id = queue.submit("gpu", "motion_video", payload(tmp_path), max_attempts=2)

    def failed(job):
        raise TimeoutError("Offline timed out")

    worker = Worker(
        queue,
        "gpu",
        {"motion_video": failed},
        failure_guard=partial(verify_remote_release, transport=transport),
    )
    assert worker.run_once()
    assert queue.get(motion_id).status == JobStatus.QUEUED
    assert queue.claim_next("gpu").id == motion_id
    assert calls == [("GET", "/history/offline-prompt"), ("GET", "/queue")]


@pytest.mark.parametrize("bad", ["unknown", "wrong_server", "corrupt", "missing_profile"])
def test_unknown_or_invalid_journal_never_releases_lane(tmp_path, queue, bad):
    record = submission(prompt_id=None) if bad == "unknown" else submission()
    if bad == "wrong_server":
        record["server"] = "http://127.0.0.1:9999"
    journal = write_journal(tmp_path, record)
    if bad == "corrupt":
        journal.write_text("{", encoding="utf-8")
    body = payload(tmp_path)
    if bad == "missing_profile":
        body["recipe"] = {}
    job_id = queue.submit("gpu", "motion_video", body)
    claimed = queue.claim_next("gpu")
    transport, calls = offline_server()
    with pytest.raises(ProcessCleanupError, match="Remote termination could not be verified"):
        verify_remote_release(claimed, transport=transport)
    assert claimed.id == job_id
    assert not calls


@pytest.mark.parametrize("failure", ["network", "invalid_queue", "invalid_history"])
def test_unverifiable_remote_server_is_unsafe(tmp_path, queue, failure):
    write_journal(tmp_path, submission())
    queue.submit("gpu", "motion_video", payload(tmp_path))
    claimed = queue.claim_next("gpu")

    def handle(request):
        if failure == "network":
            raise httpx.ConnectError("offline server unavailable", request=request)
        if request.url.path.startswith("/history/"):
            return httpx.Response(200, json=[] if failure == "invalid_history" else {})
        return httpx.Response(200, json={"queue_running": []})

    with pytest.raises(ProcessCleanupError, match="Remote termination could not be verified"):
        verify_remote_release(claimed, transport=httpx.MockTransport(handle))


def test_no_journal_means_no_remote_submission(tmp_path, queue):
    queue.submit("gpu", "motion_video", payload(tmp_path))
    claimed = queue.claim_next("gpu")
    transport, calls = offline_server()
    verify_remote_release(claimed, transport=transport)
    assert not calls


@pytest.mark.parametrize("stop_worker", [False, True])
def test_parent_checks_remote_after_isolated_timeout_or_shutdown(tmp_path, queue, stop_worker):
    motion_id = queue.submit("gpu", "motion_video", payload(tmp_path), max_attempts=1)
    seed_id = queue.submit("gpu", "singing_voice", {})
    transport, calls = offline_server(running=True)
    stop = Event()
    worker = Worker(
        queue,
        "gpu",
        {"motion_video": remote_handler},
        runner=ProcessRunner(),
        timeout_seconds=3,
        shutdown_grace_seconds=0.1,
        failure_guard=partial(verify_remote_release, transport=transport),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(worker.run_once, stop)
        journal = tmp_path / payload(tmp_path)["output_dir"] / "remote-submission.json"
        deadline = time.monotonic() + 8
        while not journal.exists():
            assert time.monotonic() < deadline
            time.sleep(0.025)
        if stop_worker:
            stop.set()
        with pytest.raises(ProcessCleanupError, match="still running or queued"):
            pending.result(timeout=10)
    assert queue.get(motion_id).status == JobStatus.RUNNING
    assert queue.get(seed_id).attempts == 0
    assert queue.claim_next("gpu") is None
    assert calls == [("GET", "/history/offline-prompt"), ("GET", "/queue")]


def test_unexpected_failure_guard_error_also_quarantines(queue):
    motion_id = queue.submit("gpu", "motion_video", {})

    def failed(job):
        raise TimeoutError("Offline timeout")

    def broken_guard(job):
        raise RuntimeError("Offline verification crash")

    worker = Worker(queue, "gpu", {"motion_video": failed}, failure_guard=broken_guard)
    with pytest.raises(ProcessCleanupError, match="verification failed"):
        worker.run_once()
    assert queue.get(motion_id).status == JobStatus.RUNNING
    assert queue.claim_next("gpu") is None
