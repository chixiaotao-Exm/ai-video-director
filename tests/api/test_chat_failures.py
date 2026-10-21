"""Synchronous provider failures always use the browser's JSON error contract."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from avd.api.app import create_app
from avd.config import Settings
from avd.director.turnlock import TurnBusy
from avd.jobs.failures import classify_failure
from avd.llm import BudgetExceeded, ChatResponse, FakeProvider
from avd.llm.providers import ProviderConfigurationError
from avd.llm.providers.base import ProviderResponseError
from avd.runtime import Runtime
from avd.state.models import Project


class OfflineProviderStatusError(RuntimeError):
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        super().__init__(text)


@pytest.mark.parametrize(
    "error,status,code,upstream",
    [
        (
            OfflineProviderStatusError(429, "QUOTA_EXHAUSTED sk-offline-hidden-key"),
            503,
            "quota_exhausted",
            429,
        ),
        (OfflineProviderStatusError(429, "Too many requests"), 503, "rate_limited", 429),
        (
            OfflineProviderStatusError(401, "Bearer hidden-access-value"),
            502,
            "authentication_failed",
            401,
        ),
        (OfflineProviderStatusError(400, "invalid model"), 502, "invalid_request", 400),
        (OfflineProviderStatusError(503, "upstream not available"), 503, "http_error", 503),
        (
            OfflineProviderStatusError(524, "<html>gateway timeout hidden-body</html>"),
            504,
            "timeout",
            524,
        ),
        (httpx.ReadTimeout("secret=hidden-timeout-value"), 504, "timeout", None),
        (httpx.ConnectError("password=hidden-password"), 503, "connection_failed", None),
        (TurnBusy("busy internal path"), 503, "director_busy", None),
        (
            ProviderConfigurationError("missing key hidden-access-value"),
            503,
            "provider_configuration",
            None,
        ),
        (
            ProviderResponseError("raw provider hidden-body", ChatResponse(content="bad")),
            502,
            "invalid_provider_response",
            None,
        ),
        (
            ProviderResponseError(
                "stream stopped",
                ChatResponse(input_tokens=23, output_tokens=11),
                failure=classify_failure(httpx.ReadTimeout("hidden-timeout")),
            ),
            504,
            "timeout",
            None,
        ),
        (
            ProviderResponseError(
                "stream stopped",
                ChatResponse(),
                failure=classify_failure(ConnectionError("hidden-connection")),
            ),
            503,
            "connection_failed",
            None,
        ),
        (
            ProviderResponseError(
                "stream failed",
                ChatResponse(),
                failure=classify_failure("quota_exhausted hidden-quota"),
            ),
            503,
            "quota_exhausted",
            None,
        ),
        (
            ProviderResponseError(
                "stream failed",
                ChatResponse(),
                failure=classify_failure("rate_limit_exceeded hidden-rate"),
            ),
            503,
            "rate_limited",
            None,
        ),
        (
            ProviderResponseError(
                "stream failed",
                ChatResponse(),
                failure=classify_failure("invalid_prompt hidden-request"),
            ),
            502,
            "invalid_request",
            None,
        ),
        (RuntimeError("internal exception hidden-body"), 500, "director_failed", None),
    ],
)
def test_chat_errors_are_safe_json_with_readable_summary_and_selected_route(
    tmp_path: Path, error: Exception, status: int, code: str, upstream: int | None
) -> None:
    provider = FakeProvider([error])
    runtime = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            director_provider="openai",
            director_model="offline-director-model",
            openai_api_key=SecretStr("sk-offline-hidden-key"),
        ),
        providers={"openai": provider},
    )
    try:
        with runtime.session_factory() as session:
            project = Project(title="Offline chat failure", budget_limit=None)
            session.add(project)
            session.commit()
            project_id = project.id
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            response = client.post(f"/projects/{project_id}/chat", json={"message": "Hello"})
            assert response.status_code == status
            assert response.headers["content-type"].startswith("application/json")
            detail = response.json()["detail"]
            assert detail["summary"] and detail["code"] == code
            assert detail["upstream_status"] == upstream
            assert detail["route"] == {
                "role": "director",
                "provider": "openai",
                "model": "offline-director-model",
                "source": "settings",
            }
            assert "hidden" not in response.text
            assert "<html>" not in response.text and "Traceback" not in response.text
            if code == "quota_exhausted":
                assert detail["retryable"] is False
            assert client.get(f"/projects/{project_id}/chat").json()[0]["content"] == "Hello"
            assert len(provider.calls) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "error,status",
    [
        (ValueError("invalid request"), 422),
        (PermissionError("consent required"), 403),
        (BudgetExceeded("budget exhausted"), 409),
    ],
)
def test_chat_preserves_existing_validation_consent_and_budget_errors(
    tmp_path: Path, error: Exception, status: int
) -> None:
    provider = FakeProvider([error])
    runtime = Runtime(
        Settings(_env_file=None, data_dir=tmp_path, director_provider="openai"),
        providers={"openai": provider},
    )
    try:
        with runtime.session_factory() as session:
            project = Project(title="Validation", budget_limit=None)
            session.add(project)
            session.commit()
            project_id = project.id
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            response = client.post(f"/projects/{project_id}/chat", json={"message": "Hello"})
            assert response.status_code == status
            assert isinstance(response.json()["detail"], str)
    finally:
        runtime.close()
