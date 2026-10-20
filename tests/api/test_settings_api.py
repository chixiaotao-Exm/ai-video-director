"""Configuration persistence and connection tests use only injected offline transports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from avd.api.security import LocalOnlyMiddleware
from avd.api.settings_api import install_settings_routes
from avd.config import Settings
from avd.llm.ledger import CostEntry
from avd.llm.providers import ChatResponse, FakeProvider
from avd.runtime import Runtime
from avd.settings_store import SettingsStore, SettingsStoreError, public_settings
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def settings_api(tmp_path: Path):
    fake = FakeProvider([ChatResponse(content="OK", input_tokens=4, output_tokens=1)])
    runtime = Runtime(
        Settings(data_dir=tmp_path, _env_file=None),
        providers={"anthropic": fake, "openai": fake, "gemini": fake},
    )
    app = FastAPI()
    app.add_middleware(LocalOnlyMiddleware)
    install_settings_routes(app, lambda: runtime)
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Connection tests", budget_limit=1)
        project_id = project.id
        session.commit()
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            yield client, runtime, fake, app, project_id
    finally:
        runtime.close()


def test_settings_precedence_and_source_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AVD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AVD_DIRECTOR_MODEL", "environment")
    (tmp_path / "settings.json").write_text('{"director_model":"file"}', encoding="utf-8")
    assert Settings(_env_file=None).director_model == "file"
    explicit = Settings(_env_file=None, director_model="explicit")
    assert explicit.director_model == "explicit"
    loaded = SettingsStore(explicit).load()
    assert loaded.director_model == "explicit"
    other = tmp_path / "other"
    other.mkdir()
    assert Settings(_env_file=None, data_dir=other).director_model == "environment"


def test_dotenv_locates_settings_file(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(f'AVD_DATA_DIR="{tmp_path.as_posix()}"\nAVD_DIRECTOR_MODEL=dotenv\n')
    (tmp_path / "settings.json").write_text('{"director_model":"file"}')
    assert Settings(_env_file=env).director_model == "file"


@pytest.mark.parametrize(
    "name", ["model_prices", "llm_routes", "job_timeouts", "job_estimated_costs"]
)
def test_empty_file_mapping_replaces_environment_mapping(tmp_path: Path, monkeypatch, name: str):
    initial = {
        "model_prices": {"old": {"input_per_million": 5}},
        "llm_routes": {"director": {"provider": "openai", "model": "old"}},
        "job_timeouts": {"tts": 60},
        "job_estimated_costs": {"tts": 0.3},
    }[name]
    monkeypatch.setenv(f"AVD_{name.upper()}", json.dumps(initial))
    store = SettingsStore(Settings(_env_file=None, data_dir=tmp_path))
    assert getattr(store.load(), name)
    assert getattr(store.update({name: {}}), name) == {}
    assert getattr(Settings(_env_file=None, data_dir=tmp_path), name) == {}
    assert getattr(Settings(_env_file=None, data_dir=tmp_path, **{name: initial}), name)
    assert getattr(Settings(_env_file=None, data_dir=tmp_path, **{name: {}}), name) == {}


def test_removing_profile_does_not_restore_environment_values(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(
        "AVD_COMFYUI_PROFILES",
        json.dumps({"local": {"base_url": "http://localhost:8188"}, "old": {"timeout": 90}}),
    )
    store = SettingsStore(Settings(_env_file=None, data_dir=tmp_path))
    settings = store.update({"comfyui_profiles": {"local": {"timeout": 20}}})
    assert set(settings.comfyui_profiles) == {"local"}
    assert settings.comfyui_profile.base_url == "http://127.0.0.1:8188"
    assert settings.comfyui_profile.timeout == 20
    assert set(Settings(_env_file=None, data_dir=tmp_path).comfyui_profiles) == {"local"}
    explicit = Settings(_env_file=None, data_dir=tmp_path, comfyui_profiles={"local": {}})
    assert explicit.comfyui_profile.timeout == 600


def test_update_secret_replace_clear_and_nonsecret_fields(settings_api, monkeypatch):
    client, runtime, fake, app, _ = settings_api
    monkeypatch.setenv("AVD_OPENAI_API_KEY", "environment-hidden-key")
    response = client.put(
        "/settings",
        json={
            "openai_api_key": "secret-key-abcd",
            "default_budget_limit": 12.5,
            "log_format": "json",
        },
    )
    assert response.status_code == 200, response.text
    assert "secret-key" not in response.text
    assert response.json()["secrets"]["openai_api_key"] == {
        "configured": True,
        "masked": "••••abcd",
    }
    assert response.json()["settings"]["default_budget_limit"] == 12.5
    assert "openai_api_key" not in response.json()["settings"]
    assert runtime.settings.openai_api_key.get_secret_value() == "secret-key-abcd"
    path = runtime.settings.data_dir / "settings.json"
    assert json.loads(path.read_text())["openai_api_key"] == "secret-key-abcd"
    assert not fake.calls and not app.state.connection_checks
    client.put("/settings", json={"whisper_model": "tiny"})
    assert client.get("/settings").json()["secrets"]["openai_api_key"]["configured"]
    response = client.put("/settings", json={"openai_api_key": None})
    assert response.json()["secrets"]["openai_api_key"] == {"configured": False, "masked": ""}
    assert json.loads(path.read_text())["openai_api_key"] is None
    assert Settings(_env_file=None, data_dir=path.parent).openai_api_key is None


@pytest.mark.parametrize("key", ["a", "abcd", "abcde", "long-secret-last"])
def test_never_echo_an_entire_short_key(tmp_path: Path, key: str):
    public = public_settings(Settings(_env_file=None, data_dir=tmp_path, openai_api_key=key))
    masked = public["secrets"]["openai_api_key"]["masked"]
    assert key not in masked
    assert masked == "••••" + (key[-4:] if len(key) > 4 else "")


@pytest.mark.parametrize(
    "changes",
    [
        {"data_dir": "elsewhere"},
        {"database_url": "sqlite:///elsewhere.db"},
        {"unknown": "nope"},
        {"openai_api_key": {"secret": "sensitive-value"}},
        {"confirm_threshold": -1},
        {"comfyui_active_profile": "missing"},
        {"log_format": "sensitive-value"},
        {"job_timeouts": {"tts": 0}},
        {"job_estimated_costs": {"tts": -1}},
        {"comfyui_profiles": {"local": {"timeout": "Infinity"}}},
        {"openai_base_url": "https://username:sensitive-value@relay.example/v1"},
        {"anthropic_base_url": "https://relay.example/v1?api_key=sensitive-value"},
        {"gemini_base_url": "https://relay.example/v1#sensitive-value"},
    ],
)
def test_settings_reject_invalid_values_without_echoing(settings_api, changes):
    client, runtime, *_ = settings_api
    response = client.put("/settings", json=changes)
    assert response.status_code == 422
    assert "sensitive-value" not in response.text
    assert not (runtime.settings.data_dir / "settings.json").exists()


def test_atomic_write_failure_keeps_existing_file(tmp_path: Path, monkeypatch):
    store = SettingsStore(Settings(_env_file=None, data_dir=tmp_path))
    store.update({"director_model": "first"})
    original = store.path.read_bytes()

    def failed_replace(*args: Any) -> None:
        raise OSError("secret-content-must-never-appear")

    monkeypatch.setattr("avd.settings_store.os.replace", failed_replace)
    with pytest.raises(SettingsStoreError) as error:
        store.update({"director_model": "second", "openai_api_key": "secret-content"})
    assert "secret-content" not in str(error.value)
    assert store.path.read_bytes() == original
    assert list(tmp_path.glob(".settings-*.tmp")) == []


def test_effective_explicit_profile_conflict_does_not_write(tmp_path: Path):
    store = SettingsStore(
        Settings(_env_file=None, data_dir=tmp_path, comfyui_active_profile="local")
    )
    with pytest.raises(SettingsStoreError, match="startup overrides"):
        store.update(
            {
                "comfyui_active_profile": "remote",
                "comfyui_profiles": {"remote": {"base_url": "http://127.0.0.1:8190"}},
            }
        )
    assert not store.path.exists()
    assert store.load().comfyui_active_profile == "local"


def test_nonsecret_fields_cannot_echo_known_secret_values(tmp_path: Path):
    secret = "private-api-value"
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        openai_api_key=secret,
        openai_base_url=f"https://relay.example/{secret}/v1",
        smoke_image_prompt=f"accidental {secret}",
        model_prices={secret: {"input_per_million": 1}},
    )
    assert secret not in json.dumps(public_settings(settings))


def test_read_errors_are_sanitized_and_paths_cannot_move(tmp_path: Path):
    file = tmp_path / "settings.json"
    for contents in ['{"secret-value":', "[]", '{"data_dir":"different"}']:
        file.write_text(contents)
        with pytest.raises(SettingsStoreError) as error:
            Settings(_env_file=None, data_dir=tmp_path)
        assert "secret-value" not in str(error.value)


def test_provider_test_records_usage_and_never_returns_model_text(settings_api):
    client, runtime, fake, app, project_id = settings_api
    client.put(
        "/settings",
        json={"model_prices": {"anthropic/claude-opus-5-5": {"input_per_million": 100}}},
    )
    response = client.post("/settings/test/anthropic", json={"project_id": project_id})
    assert response.status_code == 200, response.text
    assert response.json()["ok"]
    assert "content" not in response.json()
    assert len(fake.calls) == 1
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "Reply with OK."}]
    assert app.state.connection_checks["anthropic"]["ok"]
    with runtime.session_factory() as session:
        entries = session.scalars(select(CostEntry)).all()
        assert len(entries) == 1
        assert entries[0].role == "connection_test"
        assert entries[0].input_tokens == 4
        assert entries[0].est_cost == pytest.approx(0.0004)
        assert session.get(Project, project_id).spent == pytest.approx(0.0004)


def test_provider_test_requires_project_and_model(settings_api):
    client, _, fake, _, project_id = settings_api
    assert client.post("/settings/test/anthropic", json={"project_id": "absent"}).status_code == 404
    assert client.put("/settings", json={"gemini_model": None}).status_code == 200
    assert client.post("/settings/test/gemini", json={"project_id": project_id}).status_code == 422
    assert client.post("/settings/test/unknown", json={"project_id": project_id}).status_code == 404
    assert not fake.calls


def test_provider_test_budget_denial_does_not_call_provider(settings_api):
    client, runtime, fake, _, project_id = settings_api
    with runtime.session_factory() as session:
        session.get(Project, project_id).budget_limit = 0
        session.commit()
    client.put("/settings", json={"model_prices": {"claude-opus-5-5": {"output_per_million": 100}}})
    response = client.post("/settings/test/anthropic", json={"project_id": project_id})
    assert response.status_code == 409
    assert not fake.calls


def test_provider_failure_cannot_leak_secret(settings_api):
    client, _, fake, app, project_id = settings_api
    fake.responses = [RuntimeError("Authorization: sk-real-secret-value")]
    response = client.post("/settings/test/openai", json={"project_id": project_id})
    assert response.status_code == 502
    assert "sk-real-secret-value" not in response.text
    assert not app.state.connection_checks["openai"]["ok"]


def test_provider_test_uses_only_eight_output_tokens(settings_api, monkeypatch):
    client, runtime, _, _, project_id = settings_api
    observed: dict[str, Any] = {}
    original = runtime.router

    def router(*args: Any, **kwargs: Any):
        result = original(*args, **kwargs)
        chat = result.chat

        def call(*args: Any, **kwargs: Any):
            observed["max_output_tokens"] = result.settings.llm_max_output_tokens
            return chat(*args, **kwargs)

        result.chat = call
        return result

    monkeypatch.setattr(runtime, "router", router)
    assert client.post("/settings/test/openai", json={"project_id": project_id}).status_code == 200
    assert observed["max_output_tokens"] == 8


def test_comfyui_test_is_explicit_and_uses_selected_profile(settings_api):
    client, _, _, app, _ = settings_api
    requests: list[httpx.Request] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"system": {}, "devices": []})

    app.state.comfyui_test_transport = httpx.MockTransport(reply)
    client.put(
        "/settings", json={"comfyui_profiles": {"local": {"base_url": "http://127.0.0.1:8189"}}}
    )
    assert client.get("/settings").status_code == 200
    assert requests == []
    response = client.post("/settings/test/comfyui", json={"profile": "local"})
    assert response.status_code == 200
    assert str(requests[0].url) == "http://127.0.0.1:8189/system_stats"
    assert app.state.connection_checks["comfyui:local"]["ok"]
    client.put("/settings", json={"log_format": "json"})
    assert not app.state.connection_checks


def test_comfyui_failure_sanitizes_response(settings_api):
    client, _, _, app, _ = settings_api
    app.state.comfyui_test_transport = httpx.MockTransport(
        lambda _: httpx.Response(401, json={"error": "secret-value"})
    )
    response = client.post("/settings/test/comfyui")
    assert response.status_code == 502
    assert "secret-value" not in response.text
    assert client.post("/settings/test/comfyui", json={"profile": "unknown"}).status_code == 422


def test_integrated_validation_errors_never_echo_submitted_keys(tmp_path: Path):
    pytest.importorskip("multipart")
    from avd.api.app import create_app

    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    try:
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            for body in (
                [{"openai_api_key": "private-validation-secret"}],
                {"openai_api_key": ["private-validation-secret"]},
                {"comfyui_profiles": {"local": {"base_url": "private-validation-secret"}}},
            ):
                response = client.put("/settings", json=body)
                assert response.status_code == 422
                assert "private-validation-secret" not in response.text
            response = client.post(
                "/settings/test/openai", json={"project_id": ["private-validation-secret"]}
            )
            assert response.status_code == 422
            assert "private-validation-secret" not in response.text
    finally:
        runtime.close()
