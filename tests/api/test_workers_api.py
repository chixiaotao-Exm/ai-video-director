from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.config import Settings
from avd.jobs.supervisor import WorkerSupervisor
from avd.runtime import Runtime


class FakeProcess:
    def __init__(self, stop):
        self.stop = stop
        self.pid = 123
        self.exitcode = None
        self.alive = False
        self.closed = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        if self.stop.is_set():
            self.alive = False
            self.exitcode = 0

    def terminate(self):
        self.alive = False
        self.exitcode = -15

    def kill(self):
        self.terminate()

    def close(self):
        self.closed = True


@pytest.mark.parametrize("with_workers", [False, True])
def test_browser_controls_owned_workers_and_lifespan_stops_them(tmp_path: Path, with_workers):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    processes = []

    def factory(settings, lane, stop, claim):
        process = FakeProcess(stop)
        processes.append(process)
        return process

    supervisor = WorkerSupervisor(runtime.settings, runtime.queue, process_factory=factory)
    try:
        app = create_app(runtime=runtime, with_workers=with_workers, worker_supervisor=supervisor)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            initial = client.get("/workers").json()["workers"]
            assert {item["state"] for item in initial} == {"running" if with_workers else "stopped"}
            assert client.post("/workers/all/start").status_code == 200
            assert len(processes) == 3
            assert client.post("/workers/all/start").status_code == 200
            assert len(processes) == 3
            stopped = client.post("/workers/gpu/stop").json()["workers"]
            assert next(item for item in stopped if item["lane"] == "gpu")["state"] == "stopped"
            assert next(item for item in stopped if item["lane"] == "api")["state"] == "running"
            assert client.get("/system").json()["workers"]["workers"]
            assert client.post("/workers/bogus/start").status_code == 422
            rejected = client.post("/workers/all/stop", headers={"Origin": "https://other.example"})
            assert rejected.status_code == 403
        assert all(not process.alive and process.closed for process in processes)
    finally:
        supervisor.close()
        runtime.close()


def test_worker_routes_without_startup_report_unavailable():
    client = TestClient(create_app(), base_url="http://127.0.0.1")
    assert client.get("/workers").status_code == 503
