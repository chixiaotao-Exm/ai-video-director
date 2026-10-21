"""Captured job routes and request evidence, independent of current settings."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from avd.config import LLMRoute, Settings

from .failures import classify_failure
from .models import Job, JobStatus

_ROLES = {
    "director_report": "director",
    "subagent_turn": "subagent",
    "analyze_video": "analysis",
    "write_script": "script",
    "character_sheet": "image",
    "image_gen": "image",
    "scene_reference": "image",
}


def route_snapshot(settings: Settings, role: str) -> dict[str, Any]:
    route = settings.llm_route(role)
    return {"role": role, "provider": route.provider, "model": route.model}


def captured_route(job: Job) -> tuple[str, LLMRoute] | None:
    snapshot = job.payload.get("llm_route")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("role"), str):
        return None
    if not isinstance(snapshot.get("provider"), str):
        return None
    return snapshot["role"], LLMRoute.model_validate(
        {key: snapshot.get(key) for key in ("provider", "model")}
    )


def record_request(
    job: Job,
    role: str,
    route: LLMRoute,
    *,
    phase: str | None = None,
    details: Mapping[str, Any] | None = None,
    source: str = "actual",
) -> None:
    """Append in the caller's transaction; never open a competing SQLite writer.

    Call immediately before dispatch, after the budget guard. The owning handler
    commits even on a provider error, retaining evidence of unsuccessful calls.
    """
    if source not in {"actual", "resumed"}:
        raise ValueError("Execution evidence must be an actual dispatch or a journal resume")
    payload = dict(job.payload)
    steps = [dict(item) for item in payload.get("_execution_steps", []) if isinstance(item, dict)]
    step: dict[str, Any] = {
        "role": role,
        "provider": route.provider,
        "model": route.model,
        "source": source,
        "attempt": job.attempts,
        "requested_at" if source == "actual" else "resumed_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "calls": 1 if source == "actual" else 0,
    }
    if phase is not None:
        step["phase"] = phase
    if details is not None:
        step["details"] = deepcopy(dict(details))
    identity = ("role", "provider", "model", "attempt", "source", "phase", "details")
    if steps and all(steps[-1].get(key) == step.get(key) for key in identity):
        step["calls"] = int(steps[-1].get("calls", 1)) + (1 if source == "actual" else 0)
        steps[-1] = step
    else:
        steps.append(step)
    payload["_execution_steps"] = steps
    job.payload = payload


def _performance_snapshot(
    job: Job, role: str, recipe: dict[str, Any], captured: dict[str, Any]
) -> dict[str, Any] | None:
    """A captured identity helps queued jobs, but never proves dispatch occurred."""
    if job.job_type in {
        "performance_prepare",
        "performance_mix",
        "performance_sync",
        "performance_final",
    } or (job.job_type == "vocal_separation" and recipe.get("library_stems")):
        return {
            "role": role,
            "provider": "local",
            "model": "FFmpeg",
            "phase": "deterministic",
            "source": "snapshot",
        }
    if job.job_type in {"singing_voice", "singing_model_probe"}:
        engine = recipe.get("voice_engine")
        if job.job_type == "singing_model_probe":
            engine = (
                "seed-vc"
                if job.payload.get("seed_vc_installation")
                else "rvc"
                if job.payload.get("rvc_installation")
                else None
            )
        if engine in {"seed-vc", "rvc"}:
            key = "seed_vc_runtime" if engine == "seed-vc" else "rvc_runtime"
            installation = (
                (captured.get(key) or {}).get("installation")
                or job.payload.get(
                    "seed_vc_installation" if engine == "seed-vc" else "rvc_installation"
                )
                or {}
            )
            return {
                "role": role,
                "provider": "local",
                "model": "SeedVC" if engine == "seed-vc" else "RVC",
                "source": "snapshot",
                "details": {
                    "engine": engine,
                    "runtime_revision": installation.get("revision"),
                    "voice_model_version_id": recipe.get("voice_model_version_id")
                    or job.payload.get("version_id"),
                },
            }
    if job.job_type == "vocal_separation" and isinstance(recipe.get("demucs"), dict):
        config = recipe["demucs"].get("config") or {}
        return {
            "role": role,
            "provider": "local",
            "model": config.get("model_name", "htdemucs"),
            "source": "snapshot",
        }
    if job.job_type in {"motion_video", "performance_lipsync"} and isinstance(
        recipe.get("workflow"), dict
    ):
        workflow = recipe["workflow"]
        spec = workflow.get("spec") or {}
        models = spec.get("model_versions") or {}
        return {
            "role": role,
            "provider": "comfyui",
            "model": ", ".join(models) or "ComfyUI workflow",
            "source": "snapshot",
            "details": {
                "workflow_revision": spec.get("revision"),
                "workflow_sha256": workflow.get("template_sha256"),
                "declared_model_versions": models,
                "remote_weights_verified": False,
            },
        }
    return None


def execution_data(job: Job) -> dict[str, Any]:
    """Show this attempt's evidence; a newly queued retry shows its new snapshot."""
    role = _ROLES.get(job.job_type, job.job_type)
    history = [
        dict(item) for item in job.payload.get("_execution_steps", []) if isinstance(item, dict)
    ]
    steps = [item for item in history if item.get("attempt") == job.attempts]
    if job.status == JobStatus.QUEUED:
        steps = []
    if steps:
        return {**steps[-1], "steps": steps, "history": history}
    snapshot = job.payload.get("llm_route")
    if isinstance(snapshot, dict):
        step = {
            "role": snapshot.get("role", role),
            "provider": snapshot.get("provider"),
            "model": snapshot.get("model"),
            "source": "snapshot",
        }
    else:
        recipe = job.payload.get("recipe") or {}
        captured = recipe.get("_execution") or {}
        if captured.get("image_provider") and captured.get("image_model"):
            step = {
                "role": "image",
                "provider": captured["image_provider"],
                "model": captured["image_model"],
                "source": "snapshot",
            }
        elif performance := _performance_snapshot(job, role, recipe, captured):
            step = performance
        else:
            step = {
                "role": role,
                "provider": None,
                "model": job.model_key,
                "source": "queue_marker" if job.model_key else "unknown",
            }
    return {**step, "steps": [step], "history": history}


def job_diagnostics(job: Job) -> dict[str, Any]:
    failure = classify_failure(job.error) if job.error else None
    return {
        "execution": execution_data(job),
        "failure": failure,
        "error_summary": failure["message"] if failure else None,
    }
