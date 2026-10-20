"""Project, asset, chat and progress HTTP endpoints.

Run with ``uvicorn avd.api.app:app`` after installing the ``api`` extra.
Application construction is side-effect free; its lifespan opens the runtime.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TYPE_CHECKING
import uuid

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from avd.assets.library import AssetLibrary
from avd.assets.storage import AssetStorage
from avd.config import Settings
from avd.director.review import ReviewRequired, approve, approve_budget, review_status
from avd.jobs.models import Job, JobStatus
from avd.llm.budget import BudgetExceeded, ConfirmationRequired
from avd.state.models import Character, Node, NodeKind, Project, Version, Voice
from avd.state.repo import ProjectRepo

if TYPE_CHECKING:
    from avd.runtime import Runtime


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi"})
ASSET_EXTENSIONS = AssetStorage.IMAGE_EXTENSIONS | AssetStorage.AUDIO_EXTENSIONS
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | ASSET_EXTENSIONS | {".ogg", ".aac", ".srt", ".vtt"}
UPLOAD_CHUNK_BYTES = 1024 * 1024
TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(InputModel):
    title: str = Field(min_length=1, max_length=255)
    budget_limit: float | None = Field(default=0.0, ge=0, allow_inf_nan=False)


class CharacterInput(InputModel):
    name: str = Field(min_length=1, max_length=255)
    reference_images: dict[str, str]
    appearance_prompt: str = Field(
        min_length=1,
        validation_alias=AliasChoices("appearance_prompt", "fixed_appearance_prompt"),
    )


class VoiceInput(InputModel):
    name: str = Field(min_length=1, max_length=255)
    sample_paths: list[str] = Field(
        min_length=1,
        validation_alias=AliasChoices("sample_paths", "sample_audio_paths"),
    )
    engine: str = Field(min_length=1, max_length=255)
    engine_voice_id: str | None = None
    default_params: dict[str, Any] = Field(default_factory=dict)


class ConsentInput(InputModel):
    confirmed_by: str = Field(min_length=1, max_length=255)


class AssetsInput(InputModel):
    character_id: str = Field(min_length=1)
    voice_id: str = Field(min_length=1)


class ChatInput(InputModel):
    message: str = Field(min_length=1, validation_alias=AliasChoices("message", "text"))


class RevertInput(InputModel):
    version_number: int = Field(ge=1)


class ApprovalInput(InputModel):
    stage: Literal["structure", "script", "frames", "final"]
    approved_by: str = Field(min_length=1, max_length=255)


class BudgetApprovalInput(InputModel):
    amount: float = Field(ge=0, allow_inf_nan=False)
    approved_by: str = Field(min_length=1, max_length=255)


class ToolInput(InputModel):
    args: dict[str, Any] = Field(default_factory=dict)


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return project


def _node(session: Session, project_id: str, node_id: str) -> Node:
    _project(session, project_id)
    node = session.get(Node, node_id)
    if node is None or node.project_id != project_id:
        raise HTTPException(404, "Node not found in this project")
    return node


def _columns(value: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(value, name) for name in names}


def _project_data(project: Project) -> dict[str, Any]:
    return _columns(project, (
        "id", "title", "source_video_path", "analysis_result", "character_id",
        "voice_id", "budget_limit", "spent", "created_at",
    ))


def _asset_data(asset: Character | Voice) -> dict[str, Any]:
    fields = (
        "id", "name", "consent_confirmed", "consent_confirmed_by",
        "consent_confirmed_at", "created_at",
    )
    if isinstance(asset, Character):
        fields += ("reference_images", "fixed_appearance_prompt", "lora_path")
    else:
        fields += ("sample_audio_paths", "engine", "engine_voice_id", "model_path", "default_params")
    return _columns(asset, fields)


def _version_data(version: Version) -> dict[str, Any]:
    return _columns(version, (
        "id", "node_id", "number", "recipe_snapshot", "output_paths", "recipe_hash",
        "cost", "created_at", "note",
    ))


def _node_data(node: Node) -> dict[str, Any]:
    result = _columns(node, (
        "id", "project_id", "kind", "shot_index", "recipe", "status", "current_version_id",
    ))
    version = node.current_version
    result.update({
        "depends_on": [item.id for item in node.depends_on],
        "current_version": _version_data(version) if version is not None else None,
        "output_paths": version.output_paths if version is not None else {},
    })
    return result


def _job_data(job: Job) -> dict[str, Any]:
    return _columns(job, (
        "id", "project_id", "node_id", "lane", "job_type", "status", "result", "error",
        "attempts", "max_attempts", "created_at", "started_at", "finished_at",
    ))


def _safe_path(root: Path, relative: str, allowed: frozenset[str] | set[str]) -> Path:
    """Resolve only ordinary media files below root, never links or device paths."""

    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise HTTPException(404, "Media not found")
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(part in {"..", "."} for part in parts):
        raise HTTPException(404, "Media not found")
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise HTTPException(404, "Media not found")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved.suffix.lower() not in allowed:
        raise HTTPException(404, "Media not found")
    if not resolved.is_file():
        raise HTTPException(404, "Media not found")
    return resolved


def _save_upload(
    upload: UploadFile,
    root: Path,
    allowed: frozenset[str],
    max_bytes: int,
) -> str:
    """Stream into an exclusive random file, cleaning partial uploads on failure."""

    # A filename contributes only its suffix; all storage paths are generated here.
    suffix = PurePosixPath((upload.filename or "").replace("\\", "/")).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(415, "Unsupported media file extension")
    directory = root / "uploads"
    if directory.is_symlink() or directory.is_junction():
        raise HTTPException(400, "Invalid upload directory")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{uuid.uuid4().hex}{suffix}"
    total = 0
    created = False
    try:
        with destination.open("xb") as stream:
            created = True
            while chunk := upload.file.read(UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(413, "Upload exceeds the configured size limit")
                stream.write(chunk)
            if total == 0:
                raise HTTPException(422, "Uploaded file is empty")
    except FileExistsError as exc:
        raise HTTPException(409, "Upload identifier collision; retry the upload") from exc
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination.relative_to(root).as_posix()


def create_app(settings: Settings | None = None, *, runtime: Runtime | None = None) -> FastAPI:
    """Create an isolated app, optionally using an already configured test runtime."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        owned_runtime = None
        if application.state.runtime is None:
            from avd.runtime import Runtime

            owned_runtime = Runtime(settings or Settings())
            application.state.runtime = owned_runtime
            if application.state.max_upload_bytes is None:
                application.state.max_upload_bytes = owned_runtime.settings.max_upload_bytes
        try:
            yield
        finally:
            if owned_runtime is not None:
                owned_runtime.close()
                application.state.runtime = None

    application = FastAPI(title="AI Video Director", lifespan=lifespan)
    application.state.runtime = runtime
    application.state.max_upload_bytes = runtime.settings.max_upload_bytes if runtime else None

    def current_runtime() -> Runtime:
        value = application.state.runtime
        if value is None:
            raise HTTPException(503, "Application runtime has not started")
        return value

    def media_root() -> Path:
        return current_runtime().settings.data_dir.expanduser().resolve()

    def library(session: Session) -> AssetLibrary:
        return AssetLibrary(session, AssetStorage(media_root()))

    def ensure_project(project_id: str) -> None:
        with current_runtime().session_factory() as session:
            _project(session, project_id)

    @application.exception_handler(PermissionError)
    async def permission_error(request: Request, error: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(error)})

    @application.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @application.exception_handler(LookupError)
    async def lookup_error(request: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @application.exception_handler(BudgetExceeded)
    @application.exception_handler(ConfirmationRequired)
    @application.exception_handler(ReviewRequired)
    async def budget_error(request: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @application.post("/projects", status_code=201)
    def create_project(body: ProjectInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = Project(title=body.title, budget_limit=body.budget_limit)
            session.add(project)
            session.commit()
            return _project_data(project)

    @application.get("/projects")
    def list_projects() -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            return [_project_data(project) for project in session.scalars(
                select(Project).order_by(Project.created_at, Project.id)
            )]

    @application.post("/uploads", status_code=201)
    async def upload_asset(file: UploadFile) -> dict[str, str]:
        try:
            relative = await run_in_threadpool(
                _save_upload, file, media_root(), ASSET_EXTENSIONS,
                application.state.max_upload_bytes,
            )
        finally:
            await file.close()
        return {"path": relative}

    def set_source(project_id: str, relative: str) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = _project(session, project_id)
            project.source_video_path = relative
            project.analysis_result = None
            repo = ProjectRepo(session)
            for node in project.nodes:
                if node.kind == NodeKind.ANALYSIS:
                    repo.update_recipe(node, {"source_video_path": relative})
            session.commit()
            return _project_data(project)

    @application.post("/projects/{project_id}/source", status_code=201)
    async def upload_source(project_id: str, file: UploadFile) -> dict[str, Any]:
        await run_in_threadpool(ensure_project, project_id)
        root = media_root()
        relative: str | None = None
        try:
            relative = await run_in_threadpool(
                _save_upload, file, root, VIDEO_EXTENSIONS,
                application.state.max_upload_bytes,
            )
            return await run_in_threadpool(set_source, project_id, relative)
        except BaseException:
            if relative is not None:
                (root / relative).unlink(missing_ok=True)
            raise
        finally:
            await file.close()

    @application.post("/characters", status_code=201)
    def create_character(body: CharacterInput) -> dict[str, Any]:
        images = {view: str(_safe_path(media_root(), path, AssetStorage.IMAGE_EXTENSIONS))
                  for view, path in body.reference_images.items()}
        with current_runtime().session_factory() as session:
            asset = library(session).create_character(body.name, images, body.appearance_prompt)
            session.commit()
            return _asset_data(asset)

    @application.get("/characters")
    def list_characters() -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            return [_asset_data(asset) for asset in library(session).list_characters()]

    @application.post("/voices", status_code=201)
    def create_voice(body: VoiceInput) -> dict[str, Any]:
        samples = [str(_safe_path(media_root(), path, AssetStorage.AUDIO_EXTENSIONS))
                   for path in body.sample_paths]
        with current_runtime().session_factory() as session:
            asset = library(session).create_voice(
                body.name, samples, body.engine, body.engine_voice_id,
                default_params=body.default_params,
            )
            session.commit()
            return _asset_data(asset)

    @application.get("/voices")
    def list_voices() -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            return [_asset_data(asset) for asset in library(session).list_voices()]

    def consent(asset_type: type[Character] | type[Voice], asset_id: str,
                body: ConsentInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            asset = session.get(asset_type, asset_id)
            if asset is None:
                raise HTTPException(404, "Asset not found")
            library(session).confirm_consent(asset, body.confirmed_by)
            session.commit()
            return _asset_data(asset)

    @application.post("/characters/{asset_id}/consent")
    def character_consent(asset_id: str, body: ConsentInput) -> dict[str, Any]:
        return consent(Character, asset_id, body)

    @application.post("/voices/{asset_id}/consent")
    def voice_consent(asset_id: str, body: ConsentInput) -> dict[str, Any]:
        return consent(Voice, asset_id, body)

    @application.put("/projects/{project_id}/assets")
    def attach_assets(project_id: str, body: AssetsInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = _project(session, project_id)
            character = session.get(Character, body.character_id)
            voice = session.get(Voice, body.voice_id)
            if character is None or voice is None:
                raise HTTPException(404, "Character or voice not found")
            ProjectRepo(session).attach_assets(project, character, voice)
            session.commit()
            return _project_data(project)

    @application.post("/projects/{project_id}/chat")
    def chat(project_id: str, body: ChatInput) -> dict[str, Any]:
        from avd.director.agent import DirectorAgent

        ensure_project(project_id)
        director = DirectorAgent(current_runtime())
        reply = director.chat(project_id, body.message)
        return {"reply": reply, "history": director.history(project_id)}

    @application.get("/projects/{project_id}/chat")
    def chat_history(project_id: str) -> list[dict[str, Any]]:
        from avd.director.agent import DirectorAgent

        ensure_project(project_id)
        return DirectorAgent(current_runtime()).history(project_id)

    @application.get("/projects/{project_id}/status")
    def project_status(project_id: str) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = _project(session, project_id)
            result = _project_data(project)
            result["nodes"] = [_node_data(node) for node in project.nodes]
            result["reviews"] = review_status(session, project)
            result["budget"] = {
                "limit": project.budget_limit,
                "spent": project.spent,
                "remaining": (project.budget_limit - project.spent
                              if project.budget_limit is not None else None),
            }
            return result

    @application.get("/projects/{project_id}/nodes/{node_id}/versions")
    def versions(project_id: str, node_id: str) -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            node = _node(session, project_id, node_id)
            return [_version_data(version) for version in ProjectRepo(session).list_versions(node)]

    @application.post("/projects/{project_id}/nodes/{node_id}/revert")
    def revert(project_id: str, node_id: str, body: RevertInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            node = _node(session, project_id, node_id)
            version = ProjectRepo(session).revert(node, body.version_number)
            session.commit()
            return _version_data(version)

    @application.get("/projects/{project_id}/jobs")
    def jobs(project_id: str) -> list[dict[str, Any]]:
        ensure_project(project_id)
        return [_job_data(job) for job in current_runtime().queue.list(project_id=project_id)]

    @application.post("/projects/{project_id}/approve")
    def approve_project(project_id: str, body: ApprovalInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = _project(session, project_id)
            result = approve(session, project, body.stage, body.approved_by)
            session.commit()
            return result

    @application.post("/projects/{project_id}/approve-budget")
    def approve_project_budget(project_id: str, body: BudgetApprovalInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            project = _project(session, project_id)
            result = approve_budget(session, project, body.amount, body.approved_by)
            session.commit()
            return result

    @application.post("/projects/{project_id}/tools/{tool}")
    def call_tool(project_id: str, tool: str, body: ToolInput) -> dict[str, str]:
        from avd.director.tools import DirectorTools

        ensure_project(project_id)
        return {"result": DirectorTools(current_runtime(), project_id).execute(tool, body.args)}

    @application.get("/projects/{project_id}/events")
    async def events(project_id: str, request: Request, once: bool = False) -> StreamingResponse:
        await run_in_threadpool(ensure_project, project_id)

        async def stream() -> AsyncIterator[str]:
            seen: set[str] = set()
            resume_from = request.headers.get("last-event-id")
            while True:
                rows = await run_in_threadpool(current_runtime().queue.list, project_id=project_id)
                terminal = sorted(
                    (job for job in rows if job.status in TERMINAL_STATUSES),
                    key=lambda job: ((job.finished_at or job.created_at).isoformat(), job.id),
                )
                event_rows = [(f"{job.id}:{job.attempts}:{job.status.value}", job) for job in terminal]
                if resume_from:
                    for index, (event_id, _) in enumerate(event_rows):
                        if event_id == resume_from:
                            seen.update(item[0] for item in event_rows[:index + 1])
                            break
                    resume_from = None
                for event_id, job in event_rows:
                    if event_id in seen:
                        continue
                    seen.add(event_id)
                    payload = json.dumps(jsonable_encoder(_job_data(job)), ensure_ascii=False)
                    yield f"id: {event_id}\nevent: job_finished\ndata: {payload}\n\n"
                if once or await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    @application.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        return FileResponse(_safe_path(media_root(), path, MEDIA_EXTENSIONS))

    return application


app = create_app()


__all__ = ["app", "create_app"]
