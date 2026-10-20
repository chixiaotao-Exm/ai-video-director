"""Project, asset, chat and progress HTTP endpoints.

Run with ``uvicorn avd.api.app:app`` after installing the ``api`` extra.
Application construction is side-effect free; its lifespan opens the runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, cast

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from avd.api.security import LocalOnlyMiddleware
from avd.api.settings_api import install_settings_routes
from avd.api.workers import install_worker_routes
from avd.api.workspace import install_workspace_routes, redact_public
from avd.assets.library import AssetLibrary
from avd.assets.storage import AssetStorage
from avd.config import Settings
from avd.director.review import ReviewRequired, approve, approve_budget, review_status
from avd.jobs.models import Job, JobLane, JobStatus
from avd.jobs.supervisor import WorkerSupervisor
from avd.llm.budget import BudgetExceeded, ConfirmationRequired
from avd.observability import configure_logging, log_context
from avd.state.models import Character, Node, NodeKind, Project, Version, Voice
from avd.state.repo import ProjectRepo

if TYPE_CHECKING:
    from avd.runtime import Runtime


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi"})
ASSET_EXTENSIONS = AssetStorage.IMAGE_EXTENSIONS | AssetStorage.AUDIO_EXTENSIONS
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | ASSET_EXTENSIONS | {".ogg", ".aac", ".srt", ".vtt"}
UPLOAD_CHUNK_BYTES = 1024 * 1024
TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
_LOGGER = logging.getLogger(__name__)


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectInput(InputModel):
    title: str = Field(min_length=1, max_length=255)
    mode: Literal["remake", "anime_sequel"] = "remake"
    budget_limit: float | None = Field(default=0.0, ge=0, allow_inf_nan=False)


class SeparateInput(InputModel):
    stems: bool = False
    project_id: str | None = None
    path: str | None = Field(default=None, max_length=1024)
    name: str | None = Field(default=None, max_length=200)


class CharacterInput(InputModel):
    name: str = Field(min_length=1, max_length=255)
    reference_images: dict[str, str]
    appearance_prompt: str = Field(
        min_length=1,
        validation_alias=AliasChoices("appearance_prompt", "fixed_appearance_prompt"),
    )


class CharacterSheetInput(InputModel):
    name: str = Field(min_length=1, max_length=255)
    appearance_prompt: str = Field(min_length=1)


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
    message: str = Field(default="", validation_alias=AliasChoices("message", "text"))
    attachments: list[str] = Field(default_factory=list, max_length=9)


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
    return _columns(
        project,
        (
            "id",
            "title",
            "mode",
            "source_video_path",
            "analysis_result",
            "character_id",
            "voice_id",
            "budget_limit",
            "spent",
            "created_at",
        ),
    )


def _asset_data(asset: Character | Voice) -> dict[str, Any]:
    fields: tuple[str, ...] = (
        "id",
        "name",
        "consent_confirmed",
        "consent_confirmed_by",
        "consent_confirmed_at",
        "created_at",
    )
    if isinstance(asset, Character):
        fields += ("reference_images", "fixed_appearance_prompt", "lora_path")
    else:
        fields += (
            "sample_audio_paths",
            "engine",
            "engine_voice_id",
            "model_path",
            "default_params",
        )
    return _columns(asset, fields)


def _version_data(version: Version) -> dict[str, Any]:
    return _columns(
        version,
        (
            "id",
            "node_id",
            "number",
            "recipe_snapshot",
            "output_paths",
            "recipe_hash",
            "cost",
            "created_at",
            "note",
        ),
    )


def _node_data(node: Node) -> dict[str, Any]:
    result = _columns(
        node,
        (
            "id",
            "project_id",
            "kind",
            "shot_index",
            "recipe",
            "status",
            "current_version_id",
        ),
    )
    version = node.current_version
    result.update(
        {
            "depends_on": [item.id for item in node.depends_on],
            "current_version": _version_data(version) if version is not None else None,
            "output_paths": version.output_paths if version is not None else {},
        }
    )
    return result


def _job_data(job: Job) -> dict[str, Any]:
    return _columns(
        job,
        (
            "id",
            "project_id",
            "node_id",
            "lane",
            "job_type",
            "status",
            "result",
            "error",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
        ),
    )


def _safe_path(root: Path, relative: str, allowed: frozenset[str] | set[str]) -> Path:
    """Resolve only ordinary media files below root, never links or device paths."""

    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise HTTPException(404, "Media not found")
    parts = PurePosixPath(relative).parts
    if (
        not parts
        or PurePosixPath(relative).is_absolute()
        or any(part in {"..", "."} for part in parts)
    ):
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


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    with_workers: bool = False,
    worker_supervisor: WorkerSupervisor | None = None,
) -> FastAPI:
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
        active_settings = application.state.runtime.settings
        configure_logging(active_settings.log_format, active_settings.log_level)
        supervisor = application.state.worker_supervisor
        if supervisor is None:
            active_runtime = application.state.runtime
            supervisor = WorkerSupervisor(active_runtime.settings, active_runtime.queue)
            application.state.worker_supervisor = supervisor
        try:
            if with_workers:
                supervisor.start()
            yield
        finally:
            try:
                supervisor.close()
            finally:
                if worker_supervisor is None:
                    application.state.worker_supervisor = None
                if owned_runtime is not None:
                    owned_runtime.close()
                    application.state.runtime = None

    application = FastAPI(title="AI Video Director", lifespan=lifespan)
    application.add_middleware(LocalOnlyMiddleware)
    application.state.runtime = runtime
    application.state.worker_supervisor = worker_supervisor
    application.state.with_workers = with_workers
    application.state.max_upload_bytes = runtime.settings.max_upload_bytes if runtime else None
    static_dir = Path(__file__).with_name("static")
    application.mount("/static", StaticFiles(directory=static_dir), name="static")

    @application.middleware("http")
    async def request_logging(request: Request, call_next: Any) -> Any:
        parts = request.url.path.split("/")
        project_id = None
        if len(parts) > 2 and parts[1] == "projects":
            try:
                project_id = str(uuid.UUID(parts[2]))
            except ValueError:
                pass
        started = time.monotonic()
        status = 500
        with log_context(project_id=project_id):
            try:
                response = await call_next(request)
                status = response.status_code
                if request.url.path == "/" or request.url.path.startswith("/static/"):
                    # Revalidate UI files on every load so updates are never stale.
                    response.headers["Cache-Control"] = "no-cache"
                return response
            finally:
                _LOGGER.info(
                    "HTTP request handled",
                    extra={
                        "event": "http_request",
                        "method": request.method,
                        "status": status,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    },
                )

    @application.get("/", include_in_schema=False)
    def workspace() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    def current_runtime() -> Runtime:
        value = application.state.runtime
        if value is None:
            raise HTTPException(503, "Application runtime has not started")
        return cast("Runtime", value)

    @application.get("/health")
    def health() -> JSONResponse:
        try:
            with current_runtime().session_factory() as session:
                session.execute(select(1))
        except Exception:
            _LOGGER.warning("Database health check failed", extra={"event": "health_failed"})
            return JSONResponse(
                status_code=503, content={"status": "unavailable", "database": "unavailable"}
            )
        return JSONResponse(content={"status": "ok", "database": "ok"})

    @application.get("/metrics")
    def metrics() -> dict[str, Any]:
        try:
            with current_runtime().session_factory() as session:
                depth = {lane.value: 0 for lane in JobLane}
                for lane, count in session.execute(
                    select(Job.lane, func.count())
                    .where(Job.status == JobStatus.QUEUED)
                    .group_by(Job.lane)
                ):
                    depth[lane.value] = count
                running = [
                    _columns(
                        job,
                        ("id", "project_id", "lane", "job_type", "attempts", "started_at"),
                    )
                    for job in session.scalars(
                        select(Job)
                        .where(Job.status == JobStatus.RUNNING)
                        .order_by(Job.started_at, Job.id)
                    )
                ]
                spend = dict(session.execute(select(Project.id, Project.spent)).all())
                return {
                    "queue_depth": depth,
                    "running_jobs": running,
                    "spent_by_project": spend,
                }
        except Exception as exc:
            _LOGGER.warning("Metrics query failed", extra={"event": "metrics_failed"})
            raise HTTPException(503, "Metrics are unavailable") from exc

    def media_root() -> Path:
        return current_runtime().settings.data_dir.expanduser().resolve()

    def public(value: Any) -> Any:
        active = application.state.runtime
        return redact_public(value, active.settings) if active is not None else value

    def refresh_settings(updated: Settings) -> None:
        from avd.llm import BudgetGuard

        active = current_runtime()
        active.settings = updated
        active.engine.settings = updated
        active.engine.budget_guard = BudgetGuard(updated.confirm_threshold)
        application.state.max_upload_bytes = updated.max_upload_bytes
        configure_logging(updated.log_format, updated.log_level)

    install_settings_routes(application, current_runtime, refresh_settings)
    install_workspace_routes(application, current_runtime)
    install_worker_routes(application)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # Pydantic's raw input/context can contain write-only keys from a settings form.
        errors = [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": public(errors)})

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": public(error.detail)},
            headers=error.headers,
        )

    def library(session: Session) -> AssetLibrary:
        return AssetLibrary(session, AssetStorage(media_root()))

    def ensure_project(project_id: str) -> None:
        with current_runtime().session_factory() as session:
            _project(session, project_id)

    @application.exception_handler(PermissionError)
    async def permission_error(request: Request, error: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": public(str(error))})

    @application.exception_handler(ValueError)
    async def value_error(request: Request, error: ValueError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": public(str(error))})

    @application.exception_handler(LookupError)
    async def lookup_error(request: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": public(str(error))})

    @application.exception_handler(BudgetExceeded)
    @application.exception_handler(ConfirmationRequired)
    @application.exception_handler(ReviewRequired)
    async def budget_error(request: Request, error: RuntimeError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": public(str(error))})

    @application.post("/projects", status_code=201)
    def create_project(body: ProjectInput) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            budget = (
                body.budget_limit
                if "budget_limit" in body.model_fields_set
                else current_runtime().settings.default_budget_limit
            )
            project = Project(title=body.title, mode=body.mode, budget_limit=budget)
            session.add(project)
            session.commit()
            return _project_data(project)

    @application.get("/projects")
    def list_projects() -> list[dict[str, Any]]:
        from avd.llm.ledger import CostEntry

        with current_runtime().session_factory() as session:
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
            result = []
            for project in session.scalars(
                select(Project).order_by(Project.created_at, Project.id)
            ):
                item = _project_data(project)
                item["input_tokens"], item["output_tokens"] = usage.get(project.id, (0, 0))
                result.append(item)
            return result

    @application.post("/uploads", status_code=201)
    async def upload_asset(file: UploadFile) -> dict[str, str]:
        try:
            relative = await run_in_threadpool(
                _save_upload,
                file,
                media_root(),
                ASSET_EXTENSIONS | VIDEO_EXTENSIONS,
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

    @application.get("/projects/{project_id}/mindmap")
    def project_mindmap(project_id: str) -> dict[str, Any]:
        from avd.director import mindmap

        with current_runtime().session_factory() as session:
            return mindmap.tree(session, _project(session, project_id))

    @application.post("/projects/{project_id}/source", status_code=201)
    async def upload_source(project_id: str, file: UploadFile) -> dict[str, Any]:
        await run_in_threadpool(ensure_project, project_id)
        root = media_root()
        relative: str | None = None
        try:
            relative = await run_in_threadpool(
                _save_upload,
                file,
                root,
                VIDEO_EXTENSIONS,
                application.state.max_upload_bytes,
            )
            result = await run_in_threadpool(set_source, project_id, relative)
        except BaseException:
            if relative is not None:
                (root / relative).unlink(missing_ok=True)
            raise
        finally:
            await file.close()

        def start_analysis() -> str | None:
            from avd.director.tools import DirectorTools

            try:
                queued = json.loads(
                    DirectorTools(current_runtime(), project_id).execute("analyze_video", {})
                )
            except Exception:
                _LOGGER.exception("Automatic analysis could not be queued")
                return None
            return str(queued.get("job_id")) if queued.get("job_id") else None

        # Upload means "watch this": analysis starts at once and the director reports back.
        result["analysis_job_id"] = await run_in_threadpool(start_analysis)
        return result

    @application.get("/media-assets")
    def list_media_assets() -> list[dict[str, Any]]:
        from avd.assets.media import asset_data, list_assets

        with current_runtime().session_factory() as session:
            return [asset_data(asset) for asset in list_assets(session)]

    @application.delete("/media-assets/{asset_id}", status_code=204)
    def delete_media_asset(asset_id: str) -> None:
        from avd.assets.media import delete_asset

        with current_runtime().session_factory() as session:
            try:
                delete_asset(session, media_root(), asset_id)
            except KeyError:
                raise HTTPException(404, "Media asset not found") from None
            session.commit()

    @application.post("/media-assets/separate", status_code=201)
    def separate_media(body: SeparateInput) -> list[dict[str, Any]]:
        from avd.assets.media import MediaError, asset_data, separate

        root = media_root()
        with current_runtime().session_factory() as session:
            project = _project(session, body.project_id) if body.project_id else None
            relative = body.path or (project.source_video_path if project else None)
            if not relative:
                raise HTTPException(422, "Choose a video file or a project with a reference video")
            source = _safe_path(root, relative, VIDEO_EXTENSIONS)
            name = body.name or (project.title if project else Path(relative).stem)
            try:
                created = separate(
                    session,
                    root,
                    source.relative_to(root).as_posix(),
                    name=name,
                    project_id=project.id if project else None,
                    stems=body.stems,
                )
            except MediaError as exc:
                raise HTTPException(422, str(exc)) from None
            session.commit()
            return [asset_data(asset) for asset in created]

    @application.post("/characters", status_code=201)
    def create_character(body: CharacterInput) -> dict[str, Any]:
        images = {
            view: str(_safe_path(media_root(), path, AssetStorage.IMAGE_EXTENSIONS))
            for view, path in body.reference_images.items()
        }
        with current_runtime().session_factory() as session:
            asset = library(session).create_character(body.name, images, body.appearance_prompt)
            session.commit()
            return _asset_data(asset)

    @application.get("/characters")
    def list_characters() -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            return [_asset_data(asset) for asset in library(session).list_characters()]

    @application.post("/projects/{project_id}/character-sheet", status_code=202)
    def character_sheet(project_id: str, body: CharacterSheetInput) -> dict[str, str]:
        from avd.assets.sheets import submit_character_sheet

        ensure_project(project_id)
        job_id = submit_character_sheet(
            current_runtime(), project_id, body.name, body.appearance_prompt
        )
        return {"job_id": job_id, "status": "queued"}

    @application.post("/voices", status_code=201)
    def create_voice(body: VoiceInput) -> dict[str, Any]:
        samples = [
            str(_safe_path(media_root(), path, AssetStorage.AUDIO_EXTENSIONS))
            for path in body.sample_paths
        ]
        with current_runtime().session_factory() as session:
            asset = library(session).create_voice(
                body.name,
                samples,
                body.engine,
                body.engine_voice_id,
                default_params=body.default_params,
            )
            session.commit()
            return _asset_data(asset)

    @application.get("/voices")
    def list_voices() -> list[dict[str, Any]]:
        with current_runtime().session_factory() as session:
            return [_asset_data(asset) for asset in library(session).list_voices()]

    def consent(
        asset_type: type[Character] | type[Voice], asset_id: str, body: ConsentInput
    ) -> dict[str, Any]:
        with current_runtime().session_factory() as session:
            asset = cast(Character | Voice | None, session.get(asset_type, asset_id))
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
        from avd.director.tools import media_kind

        ensure_project(project_id)
        attachments = []
        for path in body.attachments:
            kind = media_kind(path)
            if kind is None:
                raise HTTPException(422, "Only image, video and audio files can be sent")
            _safe_path(media_root(), path, frozenset({f".{path.rsplit('.', 1)[-1].lower()}"}))
            attachments.append({"kind": kind, "path": path, "caption": ""})
        if not body.message.strip() and not attachments:
            raise HTTPException(422, "Send a message or a file")
        director = DirectorAgent(current_runtime())
        reply = director.chat(project_id, body.message, attachments=attachments)
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
            reserved = current_runtime()._reserved(session, project.id)
            result["budget"] = {
                "limit": project.budget_limit,
                "spent": project.spent,
                "reserved": reserved,
                "remaining": (
                    project.budget_limit - project.spent - reserved
                    if project.budget_limit is not None
                    else None
                ),
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
        return cast(
            list[dict[str, Any]],
            public([_job_data(job) for job in current_runtime().queue.list(project_id=project_id)]),
        )

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
                event_rows = [
                    (f"{job.id}:{job.attempts}:{job.status.value}", job) for job in terminal
                ]
                if resume_from:
                    for index, (event_id, _) in enumerate(event_rows):
                        if event_id == resume_from:
                            seen.update(item[0] for item in event_rows[: index + 1])
                            break
                    resume_from = None
                for event_id, job in event_rows:
                    if event_id in seen:
                        continue
                    seen.add(event_id)
                    payload = json.dumps(
                        jsonable_encoder(public(_job_data(job))), ensure_ascii=False
                    )
                    yield f"id: {event_id}\nevent: job_finished\ndata: {payload}\n\n"
                if once or await request.is_disconnected():
                    break
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get("/media/{path:path}")
    def media(path: str) -> FileResponse:
        return FileResponse(_safe_path(media_root(), path, MEDIA_EXTENSIONS))

    return application


app = create_app()


__all__ = ["app", "create_app"]
