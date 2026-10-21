"""Provider quota policies work offline and also classify old process tracebacks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from avd.backends.image import (
    ImageGenerationRejectedError,
    ImageGenerationUncertainError,
    OpenAIImageBackend,
)
from avd.jobs import HandlerRegistry, JobQueue, Worker
from avd.jobs.failures import classify_failure
from avd.jobs.models import JobStatus
from avd.jobs.process import ProcessCleanupError
from avd.state.db import create_engine, create_session_factory, init_db

QUOTA_RESPONSE = {
    "error": {
        "code": 429,
        "message": "Individual quota reached. Resets in 1h8m53s.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "QUOTA_EXHAUSTED",
                "metadata": {
                    "quotaResetTimeStamp": "2026-10-21T09:44:09Z",
                    "model": "claude-opus-4-6-thinking",
                    "quotaResetDelay": "1h8m53.430644538s",
                },
            }
        ],
    }
}


@pytest.mark.parametrize("encoding", [repr, json.dumps])
def test_actual_gateway_quota_failure_preserves_absolute_reset_time(encoding) -> None:
    error = (
        "Traceback (most recent call last):\n"
        "RuntimeError: Job process failed:\n"
        "openai.RateLimitError: Error code: 429 - " + encoding(QUOTA_RESPONSE)
    )
    failure = classify_failure(error)
    assert failure["code"] == "quota_exhausted"
    assert failure["http_status"] == 429
    assert failure["retryable"] is False
    assert failure["retry_at"] == "2026-10-21T09:44:09Z"
    assert "额度" in failure["message"]
    assert "Traceback" not in failure["message"]


@pytest.mark.parametrize(
    "message",
    [
        "HTTP 429: Individual quota reached",
        "Error code: 429 - {'error': {'code': 'insufficient_quota'}}",
        "Error code: 429 - You exceeded your current quota, please check your plan",
        "Error code: 429 - {'error': {'code': 'billing_hard_limit_reached'}}",
        "Error code: 400 - Your credit balance is too low to access the Anthropic API",
    ],
)
def test_clear_account_quota_exhaustion_is_not_automatically_retried(message: str) -> None:
    assert classify_failure(message)["code"] == "quota_exhausted"
    assert classify_failure(message)["retryable"] is False


@pytest.mark.parametrize(
    "message",
    [
        "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}",
        "HTTP 429 RESOURCE_EXHAUSTED: per-minute request quota exceeded",
        "anthropic.RateLimitError: Error code: 429 - {'type':'rate_limit_error'}",
        "HTTP 429: Too many requests",
    ],
)
def test_temporary_rate_limits_remain_retryable(message: str) -> None:
    assert classify_failure(message)["code"] == "rate_limited"
    assert classify_failure(message)["retryable"] is True


def test_relative_reset_delay_in_historical_error_is_not_recalculated_from_now() -> None:
    failure = classify_failure("HTTP 429 QUOTA_EXHAUSTED quotaResetDelay='4133.430644538s'")
    assert failure["retry_at"] is None


def test_reset_timestamp_with_explicit_offset_is_normalized_to_utc() -> None:
    failure = classify_failure(
        "HTTP 429 QUOTA_EXHAUSTED {'quotaResetTimeStamp':'2026-10-21T05:44:09-04:00'}"
    )
    assert failure["retry_at"] == "2026-10-21T09:44:09Z"


@pytest.mark.parametrize("timestamp", ["bad", "2026-10-21T09:44:09"])
def test_invalid_or_timezone_free_reset_does_not_invent_a_time(timestamp: str) -> None:
    assert (
        classify_failure(f"HTTP 429 QUOTA_EXHAUSTED {{'retry_at':'{timestamp}'}}")["retry_at"]
        is None
    )


def test_sdk_shaped_exception_body_and_status_are_classified_without_leaking_payload() -> None:
    class GatewayError(Exception):
        status_code = 429
        body = QUOTA_RESPONSE

    failure = classify_failure(GatewayError("upstream included sk-offline-secret in details"))
    assert failure["code"] == "quota_exhausted"
    assert failure["retry_at"] == "2026-10-21T09:44:09Z"
    assert "sk-offline-secret" not in json.dumps(failure)


@pytest.mark.parametrize(
    "message,code,retryable",
    [
        ("HTTP 401 invalid_api_key", "authentication_failed", False),
        ("HTTP 403 forbidden", "authentication_failed", False),
        ("HTTP 400 unsupported parameter", "invalid_request", False),
        ("HTTP 422 invalid model", "invalid_request", False),
        ("HTTP 500 temporary backend error", "http_error", True),
        ("HTTP 408 upstream timed out", "timeout", True),
        ("HTTP 504 gateway timed out", "timeout", True),
        ("HTTP 522 upstream connection timeout", "timeout", True),
        ("openai.InternalServerError: Error code: 524", "timeout", True),
        ("HTTP 404 endpoint missing", "http_error", False),
        ("Request timed out", "timeout", True),
        ("Unknown media tool failure", "job_failed", True),
    ],
)
def test_unrelated_failures_retain_an_explicit_safe_classification(
    message: str, code: str, retryable: bool
) -> None:
    failure = classify_failure(message)
    assert failure["code"] == code
    assert failure["retryable"] is retryable


@pytest.mark.parametrize(
    "marker",
    [
        "avd.jobs.process.ProcessCleanupError: remote prompt still running",
        "Process cleanup failed; manual recovery required: descendant remains",
        "Remote ComfyUI execution was not verified stopped after the worker lease expired",
        "Managed worker cleanup uncertain; manual recovery required: forced worker stop",
        "Job release verification failed; manual recovery required: verification crashed",
    ],
)
@pytest.mark.parametrize("upstream", ["", "HTTP 504: gateway timeout\n", "Request timed out\n"])
def test_quarantined_execution_takes_priority_over_ordinary_retryable_failures(
    marker: str, upstream: str
) -> None:
    failure = classify_failure(upstream + marker)
    assert failure["code"] == "execution_not_stopped"
    assert failure["retryable"] is False
    assert failure["retry_at"] is None
    assert "GPU 已隔离" in failure["message"]
    assert "已停止" in failure["message"] and "独占恢复" in failure["message"]
    assert "稍后重试" not in failure["message"]


def test_live_cleanup_failure_ignores_upstream_quota_retry_timestamp() -> None:
    failure = classify_failure(
        ProcessCleanupError(
            "HTTP 429 quota_exhausted retry_at='2026-10-21T09:44:09Z'; "
            "remote prompt termination remains unknown"
        )
    )
    assert failure["code"] == "execution_not_stopped"
    assert failure["retryable"] is False
    assert failure["retry_at"] is None


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("status", [None, 524])
def test_unknown_image_outcome_stops_retries_for_live_and_persisted_errors(
    persisted: bool, status: int | None
) -> None:
    error = ImageGenerationUncertainError(status_code=status)
    failure = classify_failure(
        "Job process failed:\nTraceback:\n"
        f"avd.backends.image.ImageGenerationUncertainError: {error}"
        if persisted
        else error
    )
    assert failure["code"] == "image_outcome_unknown"
    assert failure["retryable"] is False
    assert failure["http_status"] == status
    assert failure["retry_at"] is None
    assert "手动重试" in failure["message"]
    assert "Traceback" not in failure["message"]


def test_explicit_hard_quota_remains_the_primary_failure_reason() -> None:
    failure = classify_failure(
        "ImageGenerationUncertainError: HTTP 524\ncaused by Error code: 429 - insufficient_quota"
    )
    assert failure["code"] == "quota_exhausted"
    assert failure["retryable"] is False
    assert failure["http_status"] == 429


@pytest.mark.parametrize("persisted", [False, True])
def test_image_safety_rejection_is_safe_and_not_retryable(persisted: bool) -> None:
    error = ImageGenerationRejectedError()
    failure = classify_failure(
        f"Job process failed:\navd.backends.image.ImageGenerationRejectedError: {error}"
        if persisted
        else error
    )
    assert failure["code"] == "moderation_blocked"
    assert failure["retryable"] is False
    assert failure["http_status"] is None and failure["retry_at"] is None
    assert "安全系统拒绝" in failure["message"]


def test_worker_does_not_resubmit_a_gateway_200_image_safety_rejection(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'image-rejection.db').as_posix()}")
    init_db(engine)
    queue = JobQueue(create_session_factory(engine))
    task_id = queue.submit("api", "character_sheet", {}, max_attempts=3)
    submissions = []

    def generate(**arguments):
        submissions.append(arguments)
        return SimpleNamespace(error={"code": "moderation_blocked", "message": "sk-offline-secret"})

    adapter = OpenAIImageBackend(
        client=SimpleNamespace(images=SimpleNamespace(generate=generate)), stream_images=False
    )

    def render(job: Any) -> dict[str, Any]:
        adapter.generate(
            prompt="Original character",
            reference_images=[],
            output_path=tmp_path / "out.png",
            model="test",
        )
        return {}

    registry = HandlerRegistry()
    registry.register("character_sheet", render)
    try:
        worker = Worker(queue, "api", registry)
        assert worker.run_once()
        saved = queue.get(task_id)
        assert saved is not None and saved.status == JobStatus.FAILED
        assert saved.attempts == 1 and saved.error is not None
        assert classify_failure(saved.error)["code"] == "moderation_blocked"
        assert "sk-offline-secret" not in saved.error
        assert not worker.run_once()
        assert len(submissions) == 1 and not (tmp_path / "out.png").exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("status", [None, 524])
def test_worker_does_not_submit_an_uncertain_paid_image_twice(
    tmp_path: Path, isolated: bool, status: int | None
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    init_db(engine)
    queue = JobQueue(create_session_factory(engine))
    task_id = queue.submit("api", "character_sheet", {}, max_attempts=3)
    calls: list[str] = []

    def fail(job: Any) -> dict[str, Any]:
        calls.append(job.id)
        raise ImageGenerationUncertainError(status_code=status)

    class IsolatedRunner:
        def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append("isolated")
            error = ImageGenerationUncertainError(status_code=status)
            raise RuntimeError(
                "Job process failed:\nTraceback:\n"
                f"avd.backends.image.ImageGenerationUncertainError: {error}"
            )

    registry = HandlerRegistry()
    registry.register("character_sheet", fail)
    worker = Worker(
        queue,
        "api",
        registry,
        runner=IsolatedRunner() if isolated else None,
        timeout_seconds=30 if isolated else None,
    )
    try:
        assert worker.run_once()
        saved = queue.get(task_id)
        assert saved is not None and saved.status == JobStatus.FAILED
        assert saved.attempts == 1
        assert saved.error is not None
        assert classify_failure(saved.error)["code"] == "image_outcome_unknown"
        assert not worker.run_once()
        assert len(calls) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("result_kind", ["empty", "invalid_base64", "nonimage", "download_error"])
def test_worker_does_not_regenerate_an_image_when_its_result_cannot_be_published(
    tmp_path: Path, result_kind: str
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'results.db').as_posix()}")
    init_db(engine)
    queue = JobQueue(create_session_factory(engine))
    task_id = queue.submit("api", "character_sheet", {}, max_attempts=3)
    submissions = []
    downloads = []

    def generate(**arguments):
        submissions.append(arguments)
        if result_kind == "empty":
            return SimpleNamespace(data=[])
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json={"invalid_base64": "not!base64", "nonimage": "SGVsbG8="}.get(
                        result_kind
                    ),
                    url="https://media.invalid/image.png"
                    if result_kind == "download_error"
                    else None,
                )
            ]
        )

    def download(request):
        downloads.append(request)
        raise httpx.ReadTimeout("media download interrupted", request=request)

    try:
        with httpx.Client(transport=httpx.MockTransport(download)) as downloader:
            adapter = OpenAIImageBackend(
                client=SimpleNamespace(images=SimpleNamespace(generate=generate)),
                download_client=downloader,
                stream_images=False,
            )

            def render(job: Any) -> dict[str, Any]:
                adapter.generate(
                    prompt="Original stage scene",
                    reference_images=[],
                    output_path=tmp_path / "result.png",
                    model="gpt-image-2.5-sunburst",
                )
                return {}

            registry = HandlerRegistry()
            registry.register("character_sheet", render)
            worker = Worker(queue, "api", registry)
            assert worker.run_once()
            saved = queue.get(task_id)
            assert saved is not None and saved.status == JobStatus.FAILED
            assert saved.attempts == 1 and saved.error is not None
            assert classify_failure(saved.error)["code"] == "image_outcome_unknown"
            assert not worker.run_once()
        assert len(submissions) == 1
        assert len(downloads) == (1 if result_kind == "download_error" else 0)
        assert not (tmp_path / "result.png").exists()
    finally:
        engine.dispose()


@pytest.mark.parametrize("isolated", [False, True])
@pytest.mark.parametrize("hard_quota", [False, True])
def test_worker_stops_hard_quota_but_retries_transient_rate_limits(
    tmp_path: Path, isolated: bool, hard_quota: bool
) -> None:
    engine = create_engine(f"sqlite:///{(tmp_path / 'jobs.db').as_posix()}")
    init_db(engine)
    queue = JobQueue(create_session_factory(engine))
    task_id = queue.submit("api", "director_report", {}, max_attempts=3)
    error = (
        "openai.RateLimitError: Error code: 429 - " + repr(QUOTA_RESPONSE)
        if hard_quota
        else "openai.RateLimitError: Error code: 429 - rate_limit_exceeded"
    )
    calls: list[str] = []

    def fail(job: Any) -> dict[str, Any]:
        calls.append(job.id)
        raise RuntimeError(error)

    class IsolatedRunner:
        def run(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append("isolated")
            raise RuntimeError("Job process failed:\nTraceback:\n" + error)

    registry = HandlerRegistry()
    registry.register("director_report", fail)
    worker = Worker(
        queue,
        "api",
        registry,
        runner=IsolatedRunner() if isolated else None,
        timeout_seconds=30 if isolated else None,
    )
    try:
        assert worker.run_once()
        saved = queue.get(task_id)
        assert saved is not None
        assert saved.status == (JobStatus.FAILED if hard_quota else JobStatus.QUEUED)
        assert saved.attempts == 1
        assert saved.error is not None and "Traceback" in saved.error
        if hard_quota:
            assert not worker.run_once()
            assert len(calls) == 1
        else:
            assert worker.run_once()
            assert len(calls) == 2
    finally:
        engine.dispose()
