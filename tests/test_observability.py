from __future__ import annotations

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from avd.config import Settings
from avd.observability import ContextFormatter, configure_logging, log_context


def test_json_logs_have_identity_and_exception_without_arbitrary_fields() -> None:
    record = logging.LogRecord("avd.test", logging.ERROR, __file__, 1, "Failed %s", ("job",), None)
    record.event = "job_failed"
    record.secret_configuration = "not-for-logs"
    try:
        raise ValueError("example failure")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    with log_context(project_id="project-1", job_id="job-1"):
        payload = json.loads(ContextFormatter("json").format(record))
    assert payload["project_id"] == "project-1" and payload["job_id"] == "job-1"
    assert payload["event"] == "job_failed" and payload["message"] == "Failed job"
    assert "ValueError: example failure" in payload["exception"]
    assert payload["timestamp"].endswith("+00:00")
    assert "not-for-logs" not in json.dumps(payload)


def test_context_is_isolated_between_threads_and_restored_after_exception() -> None:
    formatter = ContextFormatter("json")

    def snapshot() -> dict:
        return json.loads(
            formatter.format(
                logging.LogRecord("avd.test", logging.INFO, __file__, 1, "ok", (), None)
            )
        )

    def run(index: int) -> dict:
        with log_context(project_id=f"project-{index}", job_id=f"job-{index}"):
            return snapshot()

    with log_context(project_id="outer"):
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(run, range(8)))
        with pytest.raises(RuntimeError), log_context(project_id="inner"):
            raise RuntimeError("restore")
        assert snapshot()["project_id"] == "outer"
    assert snapshot()["project_id"] is None
    assert [(item["project_id"], item["job_id"]) for item in values] == [
        (f"project-{index}", f"job-{index}") for index in range(8)
    ]


def test_configuration_is_idempotent_and_logs_go_to_stderr(capsys) -> None:
    configure_logging("json", "INFO")
    configure_logging("json", "INFO")
    with log_context(job_id="one"):
        logging.getLogger("avd.test").info("single event")
    output = capsys.readouterr()
    assert output.out == ""
    lines = output.err.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["job_id"] == "one"


def test_text_logs_are_readable_with_project_and_job_ids() -> None:
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(ContextFormatter())
    with log_context(project_id="p", job_id="j"):
        handler.handle(
            logging.LogRecord("avd.test", logging.INFO, __file__, 1, "started", (), None)
        )
    assert "project_id=p job_id=j" in output.getvalue()


@pytest.mark.parametrize("field", ["job_timeout_seconds", "worker_shutdown_grace_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_worker_limits_are_rejected(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_invalid_job_type_timeouts_are_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, job_timeouts={"tts": value})


def test_all_application_environment_settings_are_documented() -> None:
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert all(f"AVD_{name.upper()}=" in example for name in Settings.model_fields)
