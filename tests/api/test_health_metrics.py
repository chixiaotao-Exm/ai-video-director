from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("multipart")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from avd.api.app import create_app
from avd.config import Settings
from avd.runtime import Runtime
from avd.state.models import Project


def test_health_and_metrics_report_persisted_state_without_running_work(tmp_path: Path) -> None:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    try:
        with runtime.session_factory() as session:
            project = Project(title="Observed", spent=1.25, budget_limit=5)
            empty = Project(title="Empty")
            session.add_all([project, empty])
            session.commit()
            project_id, empty_id = project.id, empty.id
        runtime.queue.submit(
            "gpu", "tts", {"private_prompt": "never expose"}, project_id=project_id
        )
        runtime.queue.submit("api", "image_gen", {}, project_id=project_id)
        runtime.queue.submit("api", "write_script", {}, project_id=project_id)
        running_id = runtime.queue.submit("cpu", "final", {}, project_id=project_id)
        runtime.queue.claim_next("cpu")
        cancelled = runtime.queue.submit("cpu", "final", {})
        runtime.queue.cancel(cancelled)
        with TestClient(create_app(runtime=runtime)) as client:
            healthy = client.get("/health")
            assert healthy.status_code == 200 and healthy.json() == {
                "status": "ok",
                "database": "ok",
            }
            result = client.get("/metrics")
            assert result.status_code == 200
            payload = result.json()
            assert payload["queue_depth"] == {"gpu": 1, "api": 2, "cpu": 0}
            assert [job["id"] for job in payload["running_jobs"]] == [running_id]
            assert payload["running_jobs"][0]["project_id"] == project_id
            assert payload["spent_by_project"] == {project_id: 1.25, empty_id: 0}
            assert "private_prompt" not in result.text and "never expose" not in result.text
            assert runtime.queue.get(running_id).attempts == 1
    finally:
        runtime.close()


def test_unavailable_database_returns_503_without_connection_details(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))

    def unavailable():
        raise RuntimeError("secret database connection details")

    try:
        with TestClient(create_app(runtime=runtime)) as client:
            monkeypatch.setattr(runtime, "session_factory", unavailable)
            for route in ("/health", "/metrics"):
                response = client.get(route)
                assert response.status_code == 503
                assert "secret" not in response.text
    finally:
        runtime.close()


def test_api_log_contains_project_id_without_chat_body(tmp_path: Path, capsys) -> None:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path, log_format="json"))
    try:
        with TestClient(create_app(runtime=runtime)) as client:
            project = client.post("/projects", json={"title": "Logging"}).json()
            capsys.readouterr()
            response = client.get(f"/projects/{project['id']}/status")
            assert response.status_code == 200
            output = capsys.readouterr().err
            assert project["id"] in output and '"event": "http_request"' in output
    finally:
        runtime.close()
