from __future__ import annotations

from datetime import datetime
from threading import Event

import pytest

from avd.config import Settings
from avd.jobs import ClaimLostError, JobQueue, JobStatus, Worker
from avd.jobs.models import JobLane
from avd.jobs.supervisor import ActiveClaim, WorkerSupervisor, run_worker_loop
from avd.state.db import create_engine, create_session_factory, init_db


class FakeProcess:
    def __init__(self, stop, active, *, stubborn=False, unkillable=False, fail_start=False):
        self.stop = stop
        self.active = active
        self.stubborn = stubborn
        self.unkillable = unkillable
        self.fail_start = fail_start
        self.pid = None
        self.exitcode = None
        self.alive = False
        self.starts = 0
        self.terminates = 0
        self.kills = 0
        self.closed = False
        self.joins = []

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise OSError("fake spawn failure")
        self.pid = 100 + self.starts
        self.alive = True

    def is_alive(self):
        assert not self.closed
        return self.alive

    def join(self, timeout=None):
        self.joins.append(timeout)
        if self.stop.is_set() and not self.stubborn:
            self.alive = False
            self.exitcode = 0

    def terminate(self):
        self.terminates += 1
        if not self.unkillable:
            self.alive = False
            self.exitcode = -15

    def kill(self):
        self.kills += 1
        if not self.unkillable:
            self.alive = False
            self.exitcode = -9

    def close(self):
        assert not self.alive
        self.closed = True


class FakeProcessFactory:
    def __init__(self, **options):
        self.options = options
        self.processes = []
        self.lanes = []
        self.settings = []

    def __call__(self, settings, lane, stop, active):
        process = FakeProcess(stop, active, **self.options)
        self.processes.append(process)
        self.lanes.append(lane)
        self.settings.append(settings)
        return process


@pytest.fixture
def queue(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'queue.db').as_posix()}")
    init_db(engine)
    try:
        yield JobQueue(create_session_factory(engine))
    finally:
        engine.dispose()


def make_supervisor(queue, tmp_path, **process_options):
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        worker_shutdown_grace_seconds=0.01,
    )
    factory = FakeProcessFactory(**process_options)
    return (
        WorkerSupervisor(settings, queue, process_factory=factory, cleanup_seconds=0),
        factory,
    )


def row(status, lane):
    return next(worker for worker in status["workers"] if worker["lane"] == lane)


def test_start_all_is_idempotent_and_stops_only_selected_lane(queue, tmp_path):
    supervisor, factory = make_supervisor(queue, tmp_path)
    first = supervisor.start()
    assert [item["lane"] for item in first["workers"]] == ["gpu", "api", "cpu"]
    assert all(item["state"] == "running" and item["managed"] for item in first["workers"])
    supervisor.start()
    assert len(factory.processes) == 3
    stopped = supervisor.stop("gpu")
    assert row(stopped, "gpu")["state"] == "stopped"
    assert row(stopped, "api")["state"] == "running"
    assert row(stopped, "cpu")["state"] == "running"
    assert factory.processes[0].closed
    assert not factory.processes[1].stop.is_set()
    supervisor.start("gpu")
    assert len(factory.processes) == 4
    supervisor.close()
    supervisor.close()
    assert all(process.closed for process in factory.processes)
    with pytest.raises(RuntimeError, match="shut down"):
        supervisor.start()


def test_idle_managed_worker_and_independent_queue_activity_are_distinct(queue, tmp_path):
    external_id = queue.submit("gpu", "external", {})
    external = queue.claim_next("gpu")
    supervisor, factory = make_supervisor(queue, tmp_path)
    stopped = row(supervisor.status(), "gpu")
    assert stopped["managed"] is False
    assert stopped["state"] == "stopped"
    assert stopped["running_job_ids"] == [external_id]
    started = row(supervisor.start("gpu"), "gpu")
    assert started["state"] == "running"
    assert started["active_job_id"] is None
    supervisor.close()
    remaining = queue.get(external_id)
    assert remaining.status == JobStatus.RUNNING
    assert remaining.claim_token == external.claim_token
    assert remaining.error is None
    assert factory.processes[0].terminates == 0


def test_existing_quarantine_is_visible_and_never_automatically_recovered(queue, tmp_path):
    job_id = queue.submit("gpu", "unsafe", {})
    job = queue.claim_next("gpu")
    queue.quarantine(job_id, "descendants alive", claim_token=job.claim_token)
    supervisor, _ = make_supervisor(queue, tmp_path)
    status = row(supervisor.start("gpu"), "gpu")
    assert status["quarantined_job_ids"] == [job_id]
    assert queue.recover_stale() == 0
    assert queue.claim_next("gpu") is None
    supervisor.close()
    assert queue.get(job_id).claim_token == job.claim_token


def test_hard_stop_fences_only_owned_attempt_and_blocks_late_heartbeat(queue, tmp_path):
    job_id = queue.submit("gpu", "work", {})
    owned = queue.claim_next("gpu")
    external_id = queue.submit("api", "external", {})
    external = queue.claim_next("api")
    supervisor, factory = make_supervisor(queue, tmp_path, stubborn=True)
    supervisor.start("gpu")
    factory.processes[0].active.set(owned)
    assert row(supervisor.status(), "gpu")["active_job_id"] == job_id
    stopped = row(supervisor.stop("gpu"), "gpu")
    assert stopped["state"] == "failed"
    assert stopped["quarantined_job_ids"] == [job_id]
    assert stopped["active_job_id"] is None
    assert factory.processes[0].terminates == 1
    job = queue.get(job_id)
    assert job.status == JobStatus.RUNNING
    assert job.lease_expires_at.year == datetime.max.year
    assert not queue.heartbeat(job_id, owned.claim_token)
    with pytest.raises(ClaimLostError):
        queue.complete(job_id, {}, claim_token=owned.claim_token)
    assert queue.recover_stale() == 0
    assert queue.claim_next("gpu") is None
    assert queue.get(external_id).claim_token == external.claim_token


def test_unconfirmed_termination_remains_failed_and_cannot_spawn_overlap(queue, tmp_path):
    job_id = queue.submit("gpu", "work", {})
    owned = queue.claim_next("gpu")
    supervisor, factory = make_supervisor(queue, tmp_path, stubborn=True, unkillable=True)
    supervisor.start("gpu")
    factory.processes[0].active.set(owned)
    stopped = row(supervisor.stop("gpu"), "gpu")
    assert "unconfirmed" in stopped["error"]
    assert factory.processes[0].kills == 1
    assert not factory.processes[0].closed
    supervisor.start("gpu")
    assert len(factory.processes) == 1
    assert queue.get(job_id).lease_expires_at.year == datetime.max.year
    assert queue.claim_next("gpu") is None


@pytest.mark.parametrize("exitcode", [0, 7])
def test_unexpected_worker_exit_quarantines_unfinished_attempt(queue, tmp_path, exitcode):
    job_id = queue.submit("gpu", "work", {})
    claimed = queue.claim_next("gpu")
    supervisor, factory = make_supervisor(queue, tmp_path)
    supervisor.start("gpu")
    process = factory.processes[0]
    process.active.set(claimed)
    process.alive = False
    process.exitcode = exitcode
    status = row(supervisor.status(), "gpu")
    assert status["exit_code"] == exitcode
    assert status["quarantined_job_ids"] == [job_id]
    assert process.closed
    assert queue.recover_stale() == 0


def test_old_shared_claim_does_not_fence_a_new_attempt(queue, tmp_path):
    job_id = queue.submit("gpu", "work", {})
    old = queue.claim_next("gpu")
    queue.fail(job_id, "retry", claim_token=old.claim_token)
    replacement = queue.claim_next("gpu")
    supervisor, factory = make_supervisor(queue, tmp_path, stubborn=True)
    supervisor.start("gpu")
    factory.processes[0].active.set(old)
    supervisor.stop("gpu")
    assert queue.get(job_id).claim_token == replacement.claim_token
    assert queue.get(job_id).lease_expires_at.year != datetime.max.year


def test_failed_spawn_reported_without_claiming_jobs(queue, tmp_path):
    job_id = queue.submit("cpu", "work", {})
    supervisor, factory = make_supervisor(queue, tmp_path, fail_start=True)
    status = row(supervisor.start("cpu"), "cpu")
    assert status["state"] == "failed"
    assert status["error"] == "Worker process could not be started"
    assert factory.processes[0].closed
    assert queue.get(job_id).status == JobStatus.QUEUED


@pytest.mark.parametrize("lane", ["invalid", "GPU", ""])
def test_invalid_lane_does_not_spawn_or_stop(queue, tmp_path, lane):
    supervisor, factory = make_supervisor(queue, tmp_path)
    with pytest.raises(ValueError):
        supervisor.start(lane)
    with pytest.raises(ValueError):
        supervisor.stop(lane)
    assert factory.processes == []


def test_active_claim_rejects_empty_or_partial_snapshot():
    active = ActiveClaim.create()
    assert active.get() is None
    active.storage[:2] = b'["'
    assert active.get() is None


def test_worker_loop_reloads_settings_and_preserves_model_grouping(queue, tmp_path):
    first = queue.submit("gpu", "work", {}, model_key="A")
    middle = queue.submit("gpu", "work", {}, model_key="B")
    last = queue.submit("gpu", "work", {}, model_key="A")
    stop = Event()
    active = ActiveClaim.create()
    seen = []
    closed = []
    loaded = []

    def loader(base):
        current = base.model_copy(update={"director_model": f"model-{len(loaded)}"})
        loaded.append(current)
        return current

    class FakeRuntime:
        def __init__(self, settings):
            self.queue = queue
            self.settings = settings

        def build_worker(self, lane):
            def handler(job):
                assert active.get() == (job.id, job.claim_token)
                seen.append((job.id, self.settings.director_model))
                if len(seen) == 3:
                    stop.set()
                return {}

            return Worker(queue, lane, {"work": handler})

        def close(self):
            closed.append(self.settings)

    run_worker_loop(
        Settings(_env_file=None, data_dir=tmp_path),
        JobLane.GPU,
        stop,
        active,
        runtime_factory=FakeRuntime,
        settings_loader=loader,
    )
    assert seen == [(first, "model-0"), (last, "model-1"), (middle, "model-2")]
    assert closed == loaded
    assert all(job.status == JobStatus.SUCCEEDED for job in queue.list())


def test_worker_loop_closes_runtime_on_worker_failure(queue, tmp_path):
    closed = []

    class BrokenRuntime:
        def __init__(self, settings):
            self.queue = queue

        def build_worker(self, lane):
            raise RuntimeError("fake registry failure")

        def close(self):
            closed.append(True)

    with pytest.raises(RuntimeError, match="registry failure"):
        run_worker_loop(
            Settings(_env_file=None, data_dir=tmp_path),
            JobLane.GPU,
            Event(),
            ActiveClaim.create(),
            runtime_factory=BrokenRuntime,
            settings_loader=lambda base: base,
        )
    assert closed == [True]


def test_worker_loop_uses_saved_settings_on_next_job_and_reuses_unchanged_runtime(queue, tmp_path):
    from avd.settings_store import SettingsStore

    for _ in range(3):
        queue.submit("cpu", "work", {})
    # One slot keeps the per-job order deterministic for this reload check.
    settings = Settings(_env_file=None, data_dir=tmp_path, lane_concurrency={"cpu": 1})
    store = SettingsStore(settings)
    store.update({"director_model": "first-model"})
    stop = Event()
    seen = []
    created = []
    closed = []

    class FakeRuntime:
        def __init__(self, current):
            self.queue = queue
            self.settings = current
            created.append(current.director_model)

        def build_worker(self, lane):
            def handler(job):
                seen.append(self.settings.director_model)
                if len(seen) == 1:
                    store.update({"director_model": "updated-model"})
                if len(seen) == 3:
                    stop.set()
                return {}

            return Worker(queue, lane, {"work": handler})

        def close(self):
            closed.append(self.settings.director_model)

    run_worker_loop(
        settings,
        JobLane.CPU,
        stop,
        ActiveClaim.create(),
        runtime_factory=FakeRuntime,
    )
    assert seen == ["first-model", "updated-model", "updated-model"]
    assert created == closed == ["first-model", "updated-model"]


def test_invalid_file_settings_dont_prevent_stop(queue, tmp_path):
    supervisor, factory = make_supervisor(queue, tmp_path)
    supervisor.start("cpu")
    (tmp_path / "settings.json").write_text("{invalid", encoding="utf-8")
    assert row(supervisor.stop("cpu"), "cpu")["state"] == "stopped"
    assert factory.processes[0].closed


def test_worker_control_endpoints_with_fake_processes(queue, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from avd.api.workers import install_worker_routes

    supervisor, factory = make_supervisor(queue, tmp_path)
    app = fastapi.FastAPI()
    app.state.worker_supervisor = supervisor
    install_worker_routes(app)
    with testclient.TestClient(app, base_url="http://127.0.0.1") as client:
        assert len(client.get("/workers").json()["workers"]) == 3
        assert row(client.post("/workers/gpu/start").json(), "gpu")["state"] == "running"
        assert row(client.post("/workers/gpu/stop").json(), "gpu")["state"] == "stopped"
        assert client.post("/workers/all/start").status_code == 200
        assert client.post("/workers/all/stop").status_code == 200
        assert all(process.closed for process in factory.processes)
        assert client.post("/workers/unknown/start").status_code == 422
        assert client.post("/workers/unknown/stop").status_code == 422
        supervisor.close()
        assert client.post("/workers/all/start").status_code == 409
        del app.state.worker_supervisor
        assert client.get("/workers").status_code == 503


def test_stop_all_signals_every_lane_before_waiting(queue, tmp_path):
    supervisor, factory = make_supervisor(queue, tmp_path)
    supervisor.start()
    original_join = factory.processes[0].join

    def check_all_signals(timeout=None):
        assert all(process.stop.is_set() for process in factory.processes)
        original_join(timeout)

    factory.processes[0].join = check_all_signals
    supervisor.stop()
    assert all(process.closed for process in factory.processes)


def test_worker_loop_runs_parallel_slots(queue, tmp_path):
    import threading
    import time

    for _ in range(4):
        queue.submit("api", "work", {})
    settings = Settings(_env_file=None, data_dir=tmp_path, lane_concurrency={"api": 4})
    stop = Event()
    lock = threading.Lock()
    state = {"running": 0, "peak": 0, "done": 0}

    class FakeRuntime:
        def __init__(self, current):
            self.queue = queue
            self.settings = current

        def build_worker(self, lane):
            def handler(job):
                with lock:
                    state["running"] += 1
                    state["peak"] = max(state["peak"], state["running"])
                time.sleep(0.3)
                with lock:
                    state["running"] -= 1
                    state["done"] += 1
                    if state["done"] == 4:
                        stop.set()
                return {}

            return Worker(queue, lane, {"work": handler})

        def close(self):
            pass

    run_worker_loop(settings, JobLane.API, stop, ActiveClaim.create(), runtime_factory=FakeRuntime)
    assert state["done"] == 4
    assert state["peak"] > 1
