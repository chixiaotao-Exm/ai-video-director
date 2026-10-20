"""Local settings and explicitly requested, budgeted provider connection tests."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from avd.config import LLMRoute, Settings
from avd.llm import LLMRouter
from avd.llm.budget import BudgetExceeded, ConfirmationRequired
from avd.settings_store import SettingsStore, SettingsStoreError, public_settings
from avd.state.models import Project

if TYPE_CHECKING:
    from avd.runtime import Runtime


class ConnectionTestInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    project_id: str | None = None
    profile: str | None = None


PROVIDERS = ("anthropic", "openai", "gemini", "deepseek")


def _test_model(settings: Settings, provider: str) -> str:
    field = {
        "anthropic": "director_model",
        "openai": "script_model",
        "gemini": "gemini_model",
        "deepseek": "deepseek_model",
    }[provider]
    model = getattr(settings, field)
    if not model or not str(model).strip():
        raise HTTPException(422, "Configure a model before testing this provider")
    return str(model)


def _failure_detail(exc: BaseException, settings: Settings) -> str:
    """Explain a failed call without echoing credentials."""
    status = getattr(exc, "status_code", None)
    text = " ".join(str(exc).split())[:300]
    for provider in PROVIDERS:
        secret = getattr(settings, f"{provider}_api_key", None)
        if secret is not None and secret.get_secret_value():
            text = text.replace(secret.get_secret_value(), "***")
    text = re.sub(r"(sk|key)[-_][A-Za-z0-9_-]{6,}", "***", text, flags=re.IGNORECASE)
    prefix = f"HTTP {status} " if status else ""
    return f"{prefix}{type(exc).__name__}: {text}"


def install_settings_routes(
    app: FastAPI,
    runtime_getter: Callable[[], Runtime],
    on_settings_changed: Callable[[Settings], None] | None = None,
) -> None:
    """Install routes without constructing a runtime or contacting any service."""

    app.state.connection_checks = {}

    def store() -> SettingsStore:
        existing = getattr(app.state, "settings_store", None)
        if existing is None:
            existing = SettingsStore(runtime_getter().settings)
            app.state.settings_store = existing
        return existing

    def record(provider: str, ok: bool, summary: str, profile: str | None = None) -> dict[str, Any]:
        result = {
            "provider": provider,
            "profile": profile,
            "ok": ok,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
        }
        key = f"comfyui:{profile}" if provider == "comfyui" else provider
        app.state.connection_checks[key] = result
        return result

    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return public_settings(runtime_getter().settings)

    @app.put("/settings")
    def update_settings(changes: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            settings = store().update(changes)
        except SettingsStoreError as exc:
            raise HTTPException(422, str(exc)) from None
        if on_settings_changed is not None:
            on_settings_changed(settings)
        else:
            runtime_getter().settings = settings
        # A previous successful test does not validate newly changed credentials or URLs.
        app.state.connection_checks.clear()
        return public_settings(settings) | {"restart_required": []}

    @app.get("/settings/models/{provider}")
    def list_models(provider: str) -> dict[str, Any]:
        """List model ids the configured endpoint offers, for the settings dropdowns."""
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        settings = runtime_getter().settings
        secret = getattr(settings, f"{provider}_api_key")
        if secret is None or not secret.get_secret_value():
            return {"provider": provider, "models": [], "error": "API key not set"}
        key = secret.get_secret_value()
        base = (
            getattr(settings, f"{provider}_base_url")
            or {
                "anthropic": "https://api.anthropic.com",
                "openai": "https://api.openai.com/v1",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
                "deepseek": "https://api.deepseek.com",
            }[provider]
        )
        url = f"{base}/v1/models" if provider == "anthropic" else f"{base}/models"
        headers = {"Authorization": f"Bearer {key}"}
        if provider == "anthropic":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        try:
            transport = getattr(app.state, "models_test_transport", None)
            with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                items = response.json().get("data", [])
            models = sorted({str(item["id"]) for item in items if item.get("id")})
        except Exception as exc:
            return {"provider": provider, "models": [], "error": _failure_detail(exc, settings)}
        return {"provider": provider, "models": models}

    @app.post("/settings/test/{provider}")
    def test_connection(
        provider: str, body: ConnectionTestInput = Body(default_factory=ConnectionTestInput)
    ) -> dict[str, Any]:
        runtime = runtime_getter()
        settings = runtime.settings
        if provider == "comfyui":
            name = body.profile or settings.comfyui_active_profile
            profile = settings.comfyui_profiles.get(name)
            if profile is None:
                raise HTTPException(422, "Unknown ComfyUI profile")
            try:
                transport = getattr(app.state, "comfyui_test_transport", None)
                with httpx.Client(
                    base_url=profile.base_url,
                    timeout=min(profile.timeout, 10),
                    transport=transport,
                    follow_redirects=False,
                ) as client:
                    response = client.get("/system_stats")
                    response.raise_for_status()
                    if not isinstance(response.json(), dict):
                        raise ValueError("Invalid system statistics")
            except Exception:
                result = record(
                    provider, False, "ComfyUI connection failed; check the profile", name
                )
                raise HTTPException(502, result) from None
            return record(provider, True, "ComfyUI connection succeeded", name)
        if provider not in PROVIDERS:
            raise HTTPException(404, "Unknown provider")
        model = _test_model(settings, provider)
        if not body.project_id:
            # A tiny unaccounted probe so keys can be checked before any project exists.
            probe = LLMRouter(settings.model_copy(update={"llm_max_output_tokens": 16}))
            if runtime.providers:
                probe.providers = dict(runtime.providers)
            try:
                completion = probe._provider(provider).chat(
                    model, [{"role": "user", "content": "Reply with OK."}]
                )
            except Exception as exc:
                result = record(
                    provider,
                    False,
                    "Provider connection failed: " + _failure_detail(exc, settings),
                )
                raise HTTPException(502, result) from None
            result = record(provider, True, f"Provider connection succeeded ({model})")
            result.update(
                model=model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            return result
        with runtime.session_factory() as session:
            project = session.get(Project, body.project_id)
            if project is None:
                raise HTTPException(404, "Project not found")
            router = runtime.router(session, project)
            router.settings = settings.model_copy(
                update={
                    "llm_max_output_tokens": 8,
                    "llm_routes": {
                        **settings.llm_routes,
                        "connection_test": LLMRoute(provider=provider, model=model),
                    },
                }
            )
            try:
                completion = router.chat(
                    "connection_test", [{"role": "user", "content": "Reply with OK."}]
                )
            except (BudgetExceeded, ConfirmationRequired):
                raise HTTPException(
                    409, "Project budget or confirmation does not permit this test"
                ) from None
            except Exception as exc:
                # A malformed paid response may already have recorded its usage.
                session.commit()
                result = record(
                    provider,
                    False,
                    "Provider connection failed: " + _failure_detail(exc, settings),
                )
                raise HTTPException(502, result) from None
            session.commit()
            result = record(
                provider, True, "Provider connection succeeded; usage recorded in project"
            )
            result.update(
                project_id=project.id,
                model=model,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
            )
            return result


__all__ = ["install_settings_routes"]
