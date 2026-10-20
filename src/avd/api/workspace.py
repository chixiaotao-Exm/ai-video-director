"""Read models and safe queue actions used by the browser workspace."""

from __future__ import annotations

import logging
import platform
import re
import shutil
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from avd.director.review import ReviewRequired, budget_is_confirmed, require_review
from avd.engine.rebuild import JOB_TYPES, _hash
from avd.jobs.models import Job, JobLane, JobStatus
from avd.llm.budget import (
    BudgetExceeded,
    BudgetGuard,
    ConfirmationRequired,
    acquire_project_budget,
)
from avd.llm.ledger import CostEntry
from avd.llm.reservations import reserved_cost
from avd.observability import ContextFormatter
from avd.state.consent import require_usable
from avd.state.models import Node, NodeKind, NodeStatus, Project
from avd.state.paths import relative_path
from avd.state.repo import ProjectRepo

if TYPE_CHECKING:
    from avd.config import Settings
    from avd.runtime import Runtime

_ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


class BudgetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    budget_limit: float | None = Field(ge=0, allow_inf_nan=False)


class RegenerateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["shot_frame", "shot_video", "voiceover", "lipsync", "subtitles"] = "shot_frame"


def _redact(value: str, settings: Settings) -> str:
    """Never expose credentials in diagnostic messages, including previous keys."""
    for provider in ("anthropic", "openai", "gemini"):
        secret = getattr(settings, f"{provider}_api_key", None)
        if secret is not None and secret.get_secret_value():
            value = value.replace(secret.get_secret_value(), "[REDACTED]")
    value = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]+|AIza[A-Za-z0-9_-]+)", "[REDACTED]", value)
    value = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password)[\"']?\s*[=:]\s*[\"']?)[^\s,;\"']+",
        r"\1[REDACTED]",
        value,
    )
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def redact_public(value: Any, settings: Settings) -> Any:
    """Redact credentials recursively without changing nonstring response values."""
    if isinstance(value, str):
        return _redact(value, settings)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if isinstance(key, str)
            and re.search(r"(?i)(?:api[_-]?key|token|secret|password)$", key)
            and item is not None
            else redact_public(item, settings)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_public(item, settings) for item in value]
    return value


class RecentLogs(logging.Handler):
    """Bounded in-memory application diagnostics, owned by the app lifespan."""

    def __init__(self, current_runtime: Callable[[], Runtime]) -> None:
        super().__init__()
        self.current_runtime = current_runtime
        self.lines: deque[str] = deque(maxlen=200)
        self.setFormatter(ContextFormatter("text"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            settings = self.current_runtime().settings
            # Redact before retaining, so replacing a key never exposes its old value.
            message = _redact(self.format(record), settings)
            self.lines.append(message[:2000])
        except Exception:
            # Diagnostics must never prevent a request or worker from completing.
            return

    def read(self, limit: int) -> list[str]:
        self.acquire()
        try:
            return list(self.lines)[-limit:]
        finally:
            self.release()


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _job(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


def _begin_write(session: Session) -> None:
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))


def _budget_data(session: Session, project: Project) -> dict[str, Any]:
    reserved = reserved_cost(session, project.id)
    return {
        "id": project.id,
        "title": project.title,
        "budget_limit": project.budget_limit,
        "spent": project.spent,
        "reserved": reserved,
        "remaining": None
        if project.budget_limit is None
        else project.budget_limit - project.spent - reserved,
    }


def _thumbnail(project: Project, root: Path) -> tuple[str | None, str | None]:
    candidates = sorted(
        (node for node in project.nodes if node.kind in {NodeKind.SHOT_FRAME, NodeKind.FINAL}),
        key=lambda node: (node.kind != NodeKind.SHOT_FRAME, node.shot_index or 0),
    )
    for node in candidates:
        if node.current_version is None:
            continue
        outputs = node.current_version.output_paths
        values = outputs.values() if isinstance(outputs, dict) else outputs
        for value in values:
            try:
                path = relative_path(value)
                candidate = root / path
                resolved = candidate.resolve()
                if (
                    not resolved.is_relative_to(root)
                    or not resolved.is_file()
                    or any(
                        part.is_symlink() or part.is_junction()
                        for part in (candidate, *candidate.parents)
                        if part.is_relative_to(root)
                    )
                ):
                    continue
            except (OSError, ValueError):
                continue
            suffix = resolved.suffix.lower()
            if suffix in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES:
                return path, "image" if suffix in _IMAGE_SUFFIXES else "video"
    return None, None


def _job_data(job: Job, project: Project | None, settings: Settings) -> dict[str, Any]:
    result = {
        name: getattr(job, name)
        for name in (
            "id",
            "project_id",
            "node_id",
            "lane",
            "job_type",
            "model_key",
            "status",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
        )
    }
    result["project_title"] = project.title if project else None
    result["error"] = _redact(job.error, settings)[:8000] if job.error else None
    duration = None
    if job.started_at is not None:
        started = job.started_at.replace(tzinfo=job.started_at.tzinfo or timezone.utc)
        ended = job.finished_at or datetime.now(timezone.utc)
        ended = ended.replace(tzinfo=ended.tzinfo or timezone.utc)
        duration = round(max(0.0, (ended - started).total_seconds()), 3)
    result["duration_seconds"] = duration
    return result


def _workers(application: FastAPI) -> Any:
    supervisor = getattr(application.state, "worker_supervisor", None)
    if supervisor is not None:
        return supervisor.status()
    return {lane.value: {"status": "stopped", "pid": None} for lane in JobLane}


def _service_status(application: FastAPI, runtime: Runtime) -> dict[str, Any]:
    checks = getattr(application.state, "connection_checks", {})
    comfy_check = checks.get(f"comfyui:{runtime.settings.comfyui_active_profile}", {})
    return {
        "database": "ok",
        "comfyui": {
            "profile": runtime.settings.comfyui_active_profile,
            "base_url": _redact(runtime.settings.comfyui_profile.base_url, runtime.settings),
            "status": ("ok" if comfy_check.get("ok") else "failed")
            if comfy_check
            else "unverified",
            "last_check": redact_public(comfy_check, runtime.settings) or None,
        },
        "providers": {
            provider: bool(getattr(runtime.settings, f"{provider}_api_key", None))
            for provider in ("anthropic", "openai", "gemini")
        },
        "workers": _workers(application),
    }


def _retry_preflight(runtime: Runtime, session: Session, job: Job, project: Project) -> None:
    """Reuse generation gates in the same write transaction as the reservation."""
    acquire_project_budget(project)
    if job.node_id is not None:
        node = session.get(Node, job.node_id)
        if node is None or node.project_id != project.id or node.kind not in JOB_TYPES:
            raise HTTPException(409, "Job has no supported generation node")
        if node.status == NodeStatus.CLEAN:
            raise HTTPException(409, "The node already has a current output; regenerate it instead")
        if any(parent.status != NodeStatus.CLEAN for parent in node.depends_on):
            raise HTTPException(409, "Regenerate this node's unfinished dependencies first")
        snapshot = job.payload.get("recipe", {}).get("_execution")
        if _hash(runtime.engine._effective_recipe(node, execution=snapshot)) != job.payload.get(
            "recipe_hash"
        ):
            raise HTTPException(409, "Job recipe is stale; rebuild the current project recipe")
        runtime.engine._consent(project, node)
        require_review(session, project, "script" if node.kind == NodeKind.SHOT_FRAME else "frames")
    elif job.job_type == "write_script":
        require_review(session, project, "structure")
        if project.character is None or project.voice is None:
            raise HTTPException(409, "Attach consented character and voice assets first")
        require_usable(project.character)
        require_usable(project.voice)
    elif job.job_type == "analyze_video":
        if not project.source_video_path:
            raise HTTPException(409, "Upload a source video before retrying analysis")
    elif job.job_type != "character_sheet":
        raise HTTPException(409, "This job type cannot be retried from the browser")
    duplicate = select(Job.id).where(
        Job.id != job.id,
        Job.project_id == project.id,
        Job.status.in_(_ACTIVE),
    )
    duplicate = duplicate.where(
        Job.node_id == job.node_id if job.node_id else Job.job_type == job.job_type
    )
    if session.scalar(duplicate.limit(1)) is not None:
        raise HTTPException(409, "Equivalent work is already queued or running")
    estimate = float(job.payload.get("estimated_cost", 0))
    reserved = reserved_cost(session, project.id)
    BudgetGuard(runtime.settings.confirm_threshold).check(
        project, estimate, confirmed=budget_is_confirmed(session, project, estimate + reserved)
    )
    if (
        project.budget_limit is not None
        and project.spent + reserved + estimate > project.budget_limit
    ):
        raise BudgetExceeded("Pending work plus this retry exceeds the project budget")


def install_workspace_routes(application: FastAPI, current_runtime: Callable[[], Runtime]) -> None:
    """Install browser read models without making any external connectivity checks."""
    logs = RecentLogs(current_runtime)
    original_lifespan = application.router.lifespan_context

    @asynccontextmanager
    async def logging_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with original_lifespan(app):
            logger = logging.getLogger("avd")
            logger.addHandler(logs)
            try:
                yield
            finally:
                logger.removeHandler(logs)
                logs.close()

    application.router.lifespan_context = logging_lifespan

    @application.get("/dashboard")
    def dashboard() -> dict[str, Any]:
        runtime = current_runtime()
        root = runtime.settings.data_dir.expanduser().resolve()
        with runtime.session_factory() as session:
            usage = {
                project_id: (int(incoming), int(outgoing))
                for project_id, incoming, outgoing in session.execute(
                    select(
                        CostEntry.project_id,
                        func.coalesce(func.sum(CostEntry.input_tokens), 0),
                        func.coalesce(func.sum(CostEntry.output_tokens), 0),
                    ).group_by(CostEntry.project_id)
                )
            }
            projects = []
            for project in session.scalars(select(Project).order_by(Project.created_at.desc())):
                item = _budget_data(session, project)
                item["input_tokens"], item["output_tokens"] = usage.get(project.id, (0, 0))
                item["mode"] = project.mode
                statuses = {node.status for node in project.nodes}
                item["status"] = next(
                    (
                        status.value
                        for status in (NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.DIRTY)
                        if status in statuses
                    ),
                    "clean" if statuses else "empty",
                )
                item["thumbnail_path"], item["thumbnail_kind"] = _thumbnail(project, root)
                item["created_at"] = project.created_at
                projects.append(item)
            return {"projects": projects, "system": _service_status(application, runtime)}

    @application.get("/jobs")
    def jobs(
        project_id: str | None = None,
        status: JobStatus | None = None,
        lane: JobLane | None = None,
        job_type: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        runtime = current_runtime()
        with runtime.session_factory() as session:
            statement = select(Job, Project).outerjoin(Project, Job.project_id == Project.id)
            for column, value in (
                (Job.project_id, project_id),
                (Job.status, status),
                (Job.lane, lane),
                (Job.job_type, job_type),
            ):
                if value is not None:
                    statement = statement.where(column == value)
            statement = (
                statement.order_by(Job.created_at.desc(), Job.id).offset(offset).limit(limit)
            )
            return [
                _job_data(job, project, runtime.settings)
                for job, project in session.execute(statement)
            ]

    @application.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str) -> dict[str, Any]:
        runtime = current_runtime()
        with runtime.session_factory() as session:
            _begin_write(session)
            job = _job(session, job_id)
            if job.status != JobStatus.QUEUED:
                raise HTTPException(409, "Only queued jobs can be cancelled")
            job.status = JobStatus.CANCELLED
            job.finished_at = datetime.now(timezone.utc)
            runtime.engine._publish(session, job)
            project = session.get(Project, job.project_id) if job.project_id else None
            result = _job_data(job, project, runtime.settings)
            session.commit()
            return result

    @application.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str) -> dict[str, Any]:
        runtime = current_runtime()
        with runtime.session_factory() as session:
            _begin_write(session)
            job = _job(session, job_id)
            if job.status != JobStatus.FAILED:
                raise HTTPException(409, "Only failed jobs can be retried")
            if job.project_id is None:
                raise HTTPException(409, "Job has no project")
            project = _project(session, job.project_id)
            _retry_preflight(runtime, session, job, project)
            # Keep attempt numbers and paid markers: previous work retains its cost,
            # and the next attempt writes to a fresh attempt-specific directory.
            job.status = JobStatus.QUEUED
            job.max_attempts = job.attempts + 3
            job.error = None
            job.result = None
            job.started_at = None
            job.finished_at = None
            job.claim_token = None
            job.lease_expires_at = None
            if job.node_id:
                node = session.get(Node, job.node_id)
                assert node is not None
                node.status = NodeStatus.RUNNING
            result = _job_data(job, project, runtime.settings)
            session.commit()
            return result

    @application.post("/projects/{project_id}/shots/{shot_index}/regenerate", status_code=202)
    def regenerate(project_id: str, shot_index: int, body: RegenerateInput) -> dict[str, Any]:
        runtime = current_runtime()
        runtime.engine._reconcile(project_id)
        required: str | None = None
        confirmed = False
        with runtime.session_factory() as session:
            _begin_write(session)
            project = _project(session, project_id)
            target = next(
                (
                    node
                    for node in project.nodes
                    if node.shot_index == shot_index and node.kind == body.kind
                ),
                None,
            )
            if target is None:
                raise HTTPException(404, "Shot node not found")
            require_review(
                session, project, "script" if target.kind == NodeKind.SHOT_FRAME else "frames"
            )
            runtime.engine._consent(project, target)
            if any(parent.status != NodeStatus.CLEAN for parent in target.depends_on):
                raise HTTPException(409, "Finish this node's dependencies before regenerating")
            affected: dict[str, Node] = {}
            pending = [target]
            while pending:
                node = pending.pop()
                if node.id not in affected:
                    affected[node.id] = node
                    pending.extend(node.dependents)
            if (
                session.scalar(
                    select(Job.id)
                    .where(Job.node_id.in_(affected), Job.status.in_(_ACTIVE))
                    .limit(1)
                )
                is not None
            ):
                raise HTTPException(
                    409, "Affected work is queued or running; cancel or finish it first"
                )
            acquire_project_budget(project)
            ProjectRepo(session).update_recipe(target, {"_regeneration": uuid.uuid4().hex})
            estimate = sum(
                runtime.engine.estimate_cost(node)
                for node in affected.values()
                if node.kind in JOB_TYPES
            )
            reserved = reserved_cost(session, project_id)
            confirmed = budget_is_confirmed(session, project, estimate + reserved)
            try:
                BudgetGuard(runtime.settings.confirm_threshold).check(
                    project, estimate, confirmed=confirmed
                )
                if (
                    project.budget_limit is not None
                    and project.spent + reserved + estimate > project.budget_limit
                ):
                    raise BudgetExceeded("Regeneration and pending work exceed the project budget")
            except (BudgetExceeded, ConfirmationRequired) as exc:
                required = str(exc)
            node_ids = sorted(affected)
            session.commit()
        job_ids: list[str] = []
        if required is None:
            try:
                job_ids = runtime.engine.rebuild(project_id, node_ids=node_ids, confirmed=confirmed)
            except (BudgetExceeded, ConfirmationRequired, ReviewRequired) as exc:
                required = str(exc)
        return {"job_ids": job_ids, "node_ids": node_ids, "requires_review": required}

    @application.get("/costs")
    def costs(
        project_id: str | None = None,
        role: str | None = None,
        model: str | None = None,
        provider: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        runtime = current_runtime()
        with runtime.session_factory() as session:
            filters = [
                column == value
                for column, value in (
                    (CostEntry.project_id, project_id),
                    (CostEntry.role, role),
                    (CostEntry.model, model),
                    (CostEntry.provider, provider),
                )
                if value is not None
            ]
            entries = []
            for entry, title in session.execute(
                select(CostEntry, Project.title)
                .join(Project, CostEntry.project_id == Project.id)
                .where(*filters)
                .order_by(CostEntry.created_at.desc(), CostEntry.id)
                .offset(offset)
                .limit(limit)
            ):
                item = {
                    name: getattr(entry, name)
                    for name in (
                        "id",
                        "project_id",
                        "job_id",
                        "attempt",
                        "role",
                        "provider",
                        "model",
                        "input_tokens",
                        "output_tokens",
                        "est_cost",
                        "created_at",
                    )
                }
                item["project_title"] = title
                entries.append(item)
            total = session.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(CostEntry.est_cost), 0),
                    func.coalesce(func.sum(CostEntry.input_tokens), 0),
                    func.coalesce(func.sum(CostEntry.output_tokens), 0),
                )
                .select_from(CostEntry)
                .where(*filters)
            ).one()
            groups = {}
            for name, column in (
                ("by_project", CostEntry.project_id),
                ("by_role", CostEntry.role),
                ("by_model", CostEntry.model),
            ):
                groups[name] = [
                    {"key": key, "est_cost": cost, "count": count}
                    for key, cost, count in session.execute(
                        select(column, func.sum(CostEntry.est_cost), func.count())
                        .where(*filters)
                        .group_by(column)
                        .order_by(column)
                    )
                ]
            projects = select(Project).order_by(Project.created_at.desc())
            if project_id:
                projects = projects.where(Project.id == project_id)
            return {
                "entries": entries,
                "totals": {
                    "count": total[0],
                    "est_cost": total[1],
                    "input_tokens": total[2],
                    "output_tokens": total[3],
                },
                **groups,
                "projects": [
                    _budget_data(session, project) for project in session.scalars(projects)
                ],
            }

    @application.put("/projects/{project_id}/budget")
    def edit_budget(project_id: str, body: BudgetInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            _begin_write(session)
            project = _project(session, project_id)
            acquire_project_budget(project)
            minimum = project.spent + reserved_cost(session, project_id)
            if body.budget_limit is not None and body.budget_limit < minimum:
                raise HTTPException(409, "Budget cannot be below spent plus reserved costs")
            project.budget_limit = body.budget_limit
            session.flush()
            result = _budget_data(session, project)
            session.commit()
            return result

    @application.get("/system")
    def system() -> dict[str, Any]:
        runtime = current_runtime()
        versions = {"python": platform.python_version()}
        for package in ("avd", "fastapi", "sqlalchemy"):
            try:
                versions[package] = version(package)
            except PackageNotFoundError:
                versions[package] = "unknown"
        binaries = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
        try:
            with runtime.session_factory() as session:
                queue_depth = dict.fromkeys((lane.value for lane in JobLane), 0)
                for lane, count in session.execute(
                    select(Job.lane, func.count())
                    .where(Job.status == JobStatus.QUEUED)
                    .group_by(Job.lane)
                ):
                    queue_depth[lane.value] = count
                running = [
                    _job_data(
                        job,
                        session.get(Project, job.project_id) if job.project_id else None,
                        runtime.settings,
                    )
                    for job in session.scalars(select(Job).where(Job.status == JobStatus.RUNNING))
                ]
                metrics = {
                    "queue_depth": queue_depth,
                    "running_jobs": running,
                    "spent_by_project": dict(
                        session.execute(select(Project.id, Project.spent)).all()
                    ),
                }
        except Exception as exc:
            raise HTTPException(503, "System metrics are unavailable") from exc
        return {
            **_service_status(application, runtime),
            "health": {"status": "ok", "database": "ok"},
            "metrics": metrics,
            "versions": versions,
            "ffmpeg": {"ready": all(binaries.values()), **binaries},
        }

    @application.get("/system/logs")
    def recent_logs(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
        return {"lines": logs.read(limit)}


__all__ = ["install_workspace_routes", "redact_public"]
