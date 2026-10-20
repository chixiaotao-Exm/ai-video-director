"""Command-line entry points backed by the shared application runtime."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from avd.assets.library import AssetLibrary
from avd.assets.storage import AssetStorage
from avd.config import Settings
from avd.director.agent import DirectorAgent
from avd.director.review import review_status
from avd.director.tools import CharacterInput, DirectorTools, VoiceInput, project_node
from avd.jobs import JobStatus
from avd.observability import configure_logging
from avd.runtime import Runtime
from avd.state.models import Character, NodeKind, Project, Voice
from avd.state.paths import relative_path
from avd.state.repo import ProjectRepo

VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".webm", ".mkv", ".avi"})


def _text(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("value must not be empty")
    return value


def _budget(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("budget must be finite and non-negative")
    return number


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="avd", description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Override AVD_DATA_DIR")
    parser.add_argument("--database-url", help="Override AVD_DATABASE_URL")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create storage directories and all database tables")

    projects = commands.add_parser("project", help="Manage projects")
    project_commands = projects.add_subparsers(dest="project_command", required=True)
    create = project_commands.add_parser("create")
    create.add_argument("--title", type=_text, required=True)
    create.add_argument("--budget", "--budget-limit", type=_budget, default=0.0)
    create.add_argument("--source", "--source-relative", help="Existing uploads/ video path")
    project_commands.add_parser("list")
    project_commands.add_parser("status").add_argument("project")

    analyze = commands.add_parser("analyze", help="Import a video and queue its analysis")
    analyze.add_argument("video", type=Path)
    analyze.add_argument("--project", help="Existing project; otherwise create one")
    analyze.add_argument(
        "--budget", type=_budget, default=0.0, help="Budget for a newly created project"
    )

    assets = commands.add_parser("asset", help="Create assets from data_dir/uploads")
    asset_types = assets.add_subparsers(dest="asset_type", required=True)
    character = asset_types.add_parser("character").add_subparsers(
        dest="asset_command", required=True
    )
    character_create = character.add_parser("create")
    character_create.add_argument("--name", type=_text, required=True)
    character_create.add_argument("--appearance-prompt", type=_text, required=True)
    for view in sorted(AssetLibrary.CHARACTER_VIEWS):
        character_create.add_argument(f"--{view}", required=view == "front")
    voice = asset_types.add_parser("voice").add_subparsers(dest="asset_command", required=True)
    voice_create = voice.add_parser("create")
    voice_create.add_argument("--name", type=_text, required=True)
    voice_create.add_argument("--sample", action="append", required=True)
    voice_create.add_argument("--engine", type=_text, required=True)
    voice_create.add_argument("--engine-voice-id")
    voice_create.add_argument("--speed", type=float, default=1.0)
    voice_create.add_argument("--emotion", type=_text, default="neutral")

    consent = commands.add_parser("consent").add_subparsers(dest="consent_command", required=True)
    confirm = consent.add_parser("confirm", help="Record an explicit human consent decision")
    confirm.add_argument("asset_type", choices=["character", "voice"])
    confirm.add_argument("asset_id")
    confirm.add_argument("--confirmed-by", type=_text, required=True)

    chat = commands.add_parser("chat", help="Chat with the director; /quit exits the REPL")
    chat.add_argument("project")
    chat.add_argument("--message", type=_text, help="Send one message without opening a REPL")
    rebuild = commands.add_parser("rebuild", help="Queue approved incremental generation")
    rebuild.add_argument("project")
    rebuild.add_argument("--retry-failed", action="store_true")
    versions = commands.add_parser("versions")
    versions.add_argument("project")
    versions.add_argument("node")
    revert = commands.add_parser("revert")
    revert.add_argument("project")
    revert.add_argument("node")
    revert.add_argument("version", type=int)
    commands.add_parser("jobs").add_argument("project")

    worker = commands.add_parser("worker", help="Run one durable worker lane")
    worker.add_argument("--lane", choices=["gpu", "api", "cpu"], required=True)
    worker.add_argument("--once", action="store_true")
    worker.add_argument(
        "--recover-stale", action="store_true", help="Use only after previous workers have stopped"
    )
    serve = commands.add_parser("serve", help="Serve the web workspace (requires avd[api])")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    workers = serve.add_mutually_exclusive_group()
    workers.add_argument("--with-workers", dest="with_workers", action="store_true")
    workers.add_argument("--no-workers", dest="with_workers", action="store_false")
    serve.set_defaults(with_workers=True)
    return parser


def _row(value: Any) -> dict[str, Any]:
    return {column.key: getattr(value, column.key) for column in inspect(value).mapper.column_attrs}


def _project(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"Project {project_id!r} was not found")
    return project


def _uploads(root: Path) -> Path:
    directory = root / "uploads"
    if directory.is_symlink() or directory.is_junction():
        raise ValueError("Upload directory must not be a link")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _uploaded_file(settings: Settings, value: str, allowed: frozenset[str]) -> Path:
    normalized = relative_path(value)
    parts = normalized.split("/")
    if parts[0] != "uploads":
        raise ValueError("Media paths must stay under data_dir/uploads")
    root = settings.data_dir.expanduser().resolve()
    candidate = root
    for part in parts:
        candidate = candidate / part
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("Uploaded media paths must not contain links")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root / "uploads"):
        raise ValueError("Media paths must stay under data_dir/uploads")
    if resolved.suffix.lower() not in allowed:
        raise ValueError("Unsupported media file extension")
    if not resolved.is_file():
        raise FileNotFoundError("Uploaded media file does not exist")
    return resolved


def _import_video(settings: Settings, source: Path) -> str:
    source = source.expanduser().resolve()
    if source.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("Unsupported video file extension")
    root = settings.data_dir.expanduser().resolve()
    destination = _uploads(root) / f"{uuid.uuid4().hex}{source.suffix.lower()}"
    created = False
    try:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            created = True
            total = 0
            while chunk := incoming.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise ValueError("Video exceeds the configured upload size limit")
                outgoing.write(chunk)
            if total == 0:
                raise ValueError("Video file is empty")
    except BaseException:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return destination.relative_to(root).as_posix()


def _analyze(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    if args.project:
        with runtime.session_factory() as session:
            _project(session, args.project)
        if any(
            job.job_type == "analyze_video" and job.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            for job in runtime.queue.list(project_id=args.project)
        ):
            raise ValueError("This project already has a pending analysis job")
    relative = _import_video(runtime.settings, args.video)
    try:
        with runtime.session_factory() as session:
            repo = ProjectRepo(session)
            project = (
                _project(session, args.project)
                if args.project
                else repo.create_project(args.video.stem, budget_limit=args.budget)
            )
            project.source_video_path = relative
            project.analysis_result = None
            for node in project.nodes:
                if node.kind == NodeKind.ANALYSIS:
                    repo.update_recipe(node, {"source_video_path": relative})
            session.commit()
            project_id = project.id
    except BaseException:
        (runtime.settings.data_dir.expanduser().resolve() / relative).unlink(missing_ok=True)
        raise
    result = json.loads(DirectorTools(runtime, project_id).execute("analyze_video", {}))
    return {"project_id": project_id, "source_video_path": relative, **result}


def _asset(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    with runtime.session_factory() as session:
        library = AssetLibrary(session, AssetStorage(runtime.settings.data_dir))
        if args.asset_type == "character":
            parsed = CharacterInput.model_validate(
                {
                    "name": args.name,
                    "appearance_prompt": args.appearance_prompt,
                    "reference_images": {
                        view: getattr(args, view)
                        for view in AssetLibrary.CHARACTER_VIEWS
                        if getattr(args, view)
                    },
                }
            )
            images: dict[str, str] = {
                view: str(_uploaded_file(runtime.settings, path, AssetStorage.IMAGE_EXTENSIONS))
                for view, path in parsed.reference_images.items()
            }
            asset: Character | Voice = library.create_character(
                parsed.name, images, parsed.appearance_prompt
            )
        else:
            voice = VoiceInput.model_validate(
                {
                    "name": args.name,
                    "sample_paths": args.sample,
                    "engine": args.engine,
                    "engine_voice_id": args.engine_voice_id,
                    "default_params": {"speed": args.speed, "emotion": args.emotion},
                }
            )
            samples = [
                str(_uploaded_file(runtime.settings, path, AssetStorage.AUDIO_EXTENSIONS))
                for path in voice.sample_paths
            ]
            asset = library.create_voice(
                voice.name,
                samples,
                voice.engine,
                voice.engine_voice_id,
                default_params=voice.default_params.model_dump(),
            )
        session.commit()
        return _row(asset)


def _project_command(runtime: Runtime, args: argparse.Namespace) -> Any:
    with runtime.session_factory() as session:
        if args.project_command == "list":
            return [
                _row(project)
                for project in session.scalars(
                    select(Project).order_by(Project.created_at, Project.id)
                )
            ]
        if args.project_command == "create":
            if len(args.title) > 255:
                raise ValueError("Project title must contain at most 255 characters")
            source = None
            if args.source:
                _uploaded_file(runtime.settings, args.source, VIDEO_EXTENSIONS)
                source = relative_path(args.source)
            project = ProjectRepo(session).create_project(
                args.title, source_video_path=source, budget_limit=args.budget
            )
            session.commit()
            return _row(project)
        project = _project(session, args.project)
        result = _row(project)
        result["nodes"] = [
            {**_row(node), "depends_on": [dependency.id for dependency in node.depends_on]}
            for node in project.nodes
        ]
        result["reviews"] = review_status(session, project)
        result["budget"] = json.loads(DirectorTools(runtime, project.id).execute("get_budget", {}))
        return result


def _worker(runtime: Runtime, args: argparse.Namespace) -> dict[str, Any]:
    recovered = runtime.queue.recover_stale(force=True) if args.recover_stale else 0
    worker = runtime.build_worker(args.lane)
    stop = Event()
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        for signum in previous:
            signal.signal(signum, lambda *_: stop.set())
        if args.once:
            return {"lane": args.lane, "worked": worker.run_once(stop), "recovered": recovered}
        worker.run_forever(stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return {"lane": args.lane, "status": "stopped", "recovered": recovered}


def _chat(runtime: Runtime, args: argparse.Namespace) -> Any:
    with runtime.session_factory() as session:
        _project(session, args.project)
    director = DirectorAgent(runtime)
    if args.message is not None:
        return {"reply": director.chat(args.project, args.message)}
    print("Director chat. Type /quit to exit.")
    while True:
        try:
            message = input("You: ").strip()
        except EOFError:
            break
        if message.lower() in {"/quit", "/exit"}:
            break
        if message:
            print(f"Director: {director.chat(args.project, message)}")
    return None


def _run(runtime: Runtime, args: argparse.Namespace) -> Any:
    if args.command == "init":
        _uploads(runtime.settings.data_dir.expanduser().resolve())
        return {"status": "initialized", "data_dir": str(runtime.settings.data_dir.resolve())}
    if args.command == "project":
        return _project_command(runtime, args)
    if args.command == "analyze":
        return _analyze(runtime, args)
    if args.command == "asset":
        return _asset(runtime, args)
    if args.command == "consent":
        with runtime.session_factory() as session:
            asset = (
                session.get(Character, args.asset_id)
                if args.asset_type == "character"
                else session.get(Voice, args.asset_id)
            )
            if asset is None:
                raise LookupError("Asset was not found")
            AssetLibrary(session, AssetStorage(runtime.settings.data_dir)).confirm_consent(
                asset, args.confirmed_by
            )
            session.commit()
            return _row(asset)
    if args.command == "chat":
        return _chat(runtime, args)
    if args.command == "rebuild":
        return json.loads(
            DirectorTools(runtime, args.project).execute(
                "rebuild", {"retry_failed": args.retry_failed}
            )
        )
    if args.command in {"versions", "revert", "jobs"}:
        with runtime.session_factory() as session:
            project = _project(session, args.project)
            if args.command == "jobs":
                return [_row(job) for job in runtime.queue.list(project_id=project.id)]
            node = project_node(session, project, args.node)
            repo = ProjectRepo(session)
            if args.command == "versions":
                return [_row(version) for version in repo.list_versions(node)]
            version = repo.revert(node, args.version)
            session.commit()
            return _row(version)
    if args.command == "worker":
        return _worker(runtime, args)
    if args.command == "serve":
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        try:
            import uvicorn

            from avd.api.app import create_app
        except ImportError as exc:
            raise RuntimeError("Serving requires the API extra: pip install 'avd[api]'") from exc
        uvicorn.run(
            create_app(runtime=runtime, with_workers=args.with_workers),
            host=args.host,
            port=args.port,
        )
        return None
    raise ValueError(f"Unknown command: {args.command}")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _use_bundled_ffmpeg(settings: Settings) -> None:
    """Put data_dir/tools/ffmpeg on PATH when FFmpeg is not installed system-wide.

    Worker processes inherit the environment, so media jobs find it too.
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    bundled = settings.data_dir.expanduser().resolve() / "tools" / "ffmpeg"
    if (bundled / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).is_file():
        os.environ["PATH"] = f"{bundled}{os.pathsep}{os.environ.get('PATH', '')}"


def main(argv: Sequence[str] | None = None) -> int:
    """Run a command and return a shell exit code without exposing a traceback."""
    runtime = None
    try:
        args = _parser().parse_args(argv)
        overrides: dict[str, Any] = {}
        if args.data_dir is not None:
            overrides["data_dir"] = args.data_dir.expanduser().resolve()
        if args.database_url is not None:
            overrides["database_url"] = args.database_url
        settings = Settings(**overrides)
        configure_logging(settings.log_format, settings.log_level)
        _use_bundled_ffmpeg(settings)
        runtime = Runtime(settings)
        result = _run(runtime, args)
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, default=_json_default))
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"avd: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
