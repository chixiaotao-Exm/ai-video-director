"""A completely offline, real-media demonstration of incremental rebuilding.

Run ``python -m avd.demo`` with FFmpeg and ffprobe on PATH. Each run keeps its
own database and immutable version files; only ``demo/final.mp4`` is replaced.
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import textwrap
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from avd.agents.script import ScriptAgent, Storyboard, StoryboardShot
from avd.assets import AssetLibrary, AssetStorage
from avd.backends.media import MediaProcessingError, _run, media_duration
from avd.backends.workflows import DEFAULT_WORKFLOWS_DIR
from avd.config import Settings
from avd.engine.rebuild import RebuildEngine
from avd.executors.handlers import _Execution, build_registry
from avd.jobs import HandlerRegistry, Job, JobLane, JobQueue, JobStatus, Worker
from avd.llm import FakeProvider, LLMRouter
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import NodeKind, NodeStatus, Project
from avd.state.repo import ProjectRepo

_COLORS = ("0x143b52", "0x59402d", "0x234c43")
_EDITED_LINE = "Only shot two changes. The other shots keep their original versions."


def _ffmpeg(arguments: list[str], *, cwd: Path | None = None) -> None:
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n", *arguments], cwd=cwd)


def _card(text: str, destination: Path, index: int = 1) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Neither user text nor platform-specific paths enter FFmpeg's filter syntax.
    caption = destination.with_suffix(".txt")
    with caption.open("x", encoding="utf-8") as stream:
        stream.write(text)
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c={_COLORS[(index - 1) % 3]}:s=640x360:r=30",
            "-vf",
            f"drawtext=textfile={caption.name}:expansion=none:fontcolor=white:"
            "fontsize=30:line_spacing=18:x=42:y=72",
            "-frames:v",
            "1",
            "-threads",
            "1",
            str(destination.resolve()),
        ],
        cwd=destination.parent,
    )
    return destination


def _tone(destination: Path, duration: float, frequency: int = 440) -> Path:
    _ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=48000",
            "-t",
            str(duration),
            "-af",
            "volume=0.12",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(destination.resolve()),
        ]
    )
    return destination


def _shot(execution: _Execution) -> StoryboardShot:
    storyboard = Storyboard.model_validate(execution.context.get("storyboard", {}))
    index = execution.recipe.get("shot_index")
    matches = [shot for shot in storyboard.shots if shot.index == index]
    if len(matches) != 1:
        raise ValueError("Offline generation requires one matching storyboard shot")
    return matches[0]


def build_demo_registry(settings: Settings) -> HandlerRegistry:
    """Replace model work with local media; retain the normal assembly handlers."""
    registry = build_registry(settings)

    def frame(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        shot = _shot(execution)
        execution.prepare()
        text = f"SHOT {shot.index:02d}\n\n{textwrap.fill(shot.visual_prompt, width=30)}"
        output = _card(text, execution.output_dir / "frame.png", shot.index)
        return execution.result({"image": output})

    def video(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        shot = _shot(execution)
        image = execution.input_file(execution.dependency("shot_frame"), "image")
        execution.prepare()
        output = execution.output_dir / "video.mp4"
        frames = max(1, math.ceil(shot.duration_s * 30))
        _ffmpeg(
            [
                "-i",
                str(image),
                "-vf",
                "scale=1280:720,zoompan=z='min(zoom+0.0005,1.10)':"
                f"x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d={frames}:s=640x360:fps=30",
                "-t",
                str(shot.duration_s),
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ]
        )
        return execution.result({"video": output}, duration_s=media_duration(output))

    def tts(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        shot = _shot(execution)
        execution.prepare()
        # This is deliberately an audible tone, not a cloned or synthetic person's voice.
        frequency = 330 + shot.index * 110 + sum(map(ord, shot.line)) % 100
        output = _tone(execution.output_dir / "voice.wav", shot.duration_s, frequency)
        return execution.result({"audio": output}, duration_s=media_duration(output))

    def lipsync(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        video = execution.input_file(execution.dependency("shot_video"), "video")
        audio = execution.input_file(execution.dependency("voiceover"), "audio")
        execution.prepare()
        output = execution.output_dir / "lipsync.mp4"
        _ffmpeg(
            [
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-shortest",
                str(output),
            ]
        )
        return execution.result({"video": output}, duration_s=media_duration(output))

    for name, handler in (
        ("image_gen", frame),
        ("image_to_video", video),
        ("tts", tts),
        ("lipsync", lipsync),
    ):
        registry.register(name, handler)
    return registry


def _storyboard(duration: float) -> Storyboard:
    lines = (
        ("Build three shots", "Three shots, one reproducible project."),
        ("Edit one line", "Change one line without rebuilding the other shots."),
        ("Reuse saved versions", "Cached versions keep the rest of the video intact."),
    )
    return Storyboard(
        title="Offline incremental video demo",
        shots=[
            StoryboardShot(
                index=index,
                duration_s=duration,
                line=line,
                visual_prompt=visual,
                camera="slow zoom",
                emotion="neutral",
            )
            for index, (visual, line) in enumerate(lines, start=1)
        ],
    )


def _agent(session: Session, settings: Settings) -> ScriptAgent:
    # Applying an already-authored storyboard never calls a provider. A fake also
    # makes accidental future model calls fail locally instead of reaching a network.
    router = LLMRouter(settings, providers={"openai": FakeProvider()})
    return ScriptAgent(ProjectRepo(session), router, settings)


def _create_project(
    sessions: sessionmaker[Session], settings: Settings, run_dir: Path, duration: float
) -> str:
    reference = _card("OFFLINE DEMO\n\nSynthetic character card", run_dir / "reference.png")
    sample = _tone(run_dir / "sample.wav", 0.25)
    with sessions() as session:
        assets = AssetLibrary(session, AssetStorage(settings.data_dir))
        character = assets.create_character(
            "Synthetic demo card", {"front": str(reference)}, "A simple colored title card"
        )
        voice = assets.create_voice("Synthetic demo tone", [str(sample)], "offline-sine")
        for asset in (character, voice):
            assets.confirm_consent(asset, "avd.demo: generated synthetic assets; no human identity")
        repo = ProjectRepo(session)
        project = repo.create_project("Offline three-shot demo", budget_limit=0)
        repo.attach_assets(project, character, voice)
        _agent(session, settings).apply_storyboard(project, _storyboard(duration))
        session.commit()
        return project.id


def _snapshot(sessions: sessionmaker[Session], project_id: str) -> dict[str, dict[str, Any]]:
    with sessions() as session:
        project = session.get(Project, project_id)
        assert project is not None
        return {
            node.id: {
                "label": node.kind.value + (f"[{node.shot_index}]" if node.shot_index else ""),
                "status": node.status,
                "version_id": node.current_version_id,
                "version_count": len(node.versions),
                "outputs": dict(node.current_version.output_paths) if node.current_version else {},
            }
            for node in project.nodes
        }


def _drain(engine: RebuildEngine, workers: list[Worker], project_id: str) -> None:
    for _ in range(100):
        engine.rebuild(project_id)
        progressed = False
        for worker in workers:
            progressed = worker.run_once() or progressed
        if not progressed:
            failed = engine.queue.list(project_id, JobStatus.FAILED)
            if failed:
                raise RuntimeError(
                    f"Offline demo job {failed[0].job_type} failed: {failed[0].error}"
                )
            with engine.session_factory() as session:
                project = session.get(Project, project_id)
                assert project is not None
                if any(node.status != NodeStatus.CLEAN for node in project.nodes):
                    raise RuntimeError("Offline demo stalled with unfinished nodes")
            return
    raise RuntimeError("Offline demo exceeded 100 worker cycles")


def _final_path(snapshot: dict[str, dict[str, Any]], root: Path) -> Path:
    final = next(value for value in snapshot.values() if value["label"] == "final")
    return root / str(final["outputs"]["final"])


def run_demo(
    data_dir: str | Path | None = None,
    *,
    duration_s: float = 2.0,
    output: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Build, edit, and prove cache reuse; return observations from persistent state."""
    if isinstance(duration_s, bool) or not math.isfinite(duration_s) or duration_s <= 0:
        raise ValueError("duration_s must be positive and finite")
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        raise MediaProcessingError(f"Offline demo requires {', '.join(missing)} on PATH")
    root = Path(data_dir if data_dir is not None else Settings().data_dir).expanduser().resolve()
    run_dir = root / "demo" / "runs" / uuid.uuid4().hex
    run_dir.mkdir(parents=True)
    database_path = run_dir / "state.db"
    settings = Settings(
        _env_file=None,
        data_dir=root,
        database_url=f"sqlite:///{database_path.as_posix()}",
        workflow_dir=DEFAULT_WORKFLOWS_DIR,
        image_model="offline-ffmpeg",
        job_estimated_costs={},
    )
    # The demo is a deterministic local stub; ambient paid-cost env settings
    # must never turn a no-network run into a budget failure.
    settings.job_estimated_costs = {}
    database = create_engine(settings.database_url)
    try:
        init_db(database)
        sessions = create_session_factory(database)
        project_id = _create_project(sessions, settings, run_dir, duration_s)
        queue = JobQueue(sessions)
        engine = RebuildEngine(sessions, queue, settings)
        registry = build_demo_registry(settings)
        workers = [Worker(queue, lane, registry, engine.on_finished) for lane in JobLane]
        output(f"Offline demo project: {project_id} (synthetic cards and tones)")
        _drain(engine, workers, project_id)
        initial = _snapshot(sessions, project_id)
        initial_jobs = queue.list(project_id)
        initial_nodes = [initial[str(job.node_id)]["label"] for job in initial_jobs]
        output(f"Initial build: {len(initial_nodes)} generated nodes: {', '.join(initial_nodes)}")

        with sessions() as session:
            project = session.get(Project, project_id)
            assert project is not None
            _agent(session, settings).update_shot(project, 2, {"line": _EDITED_LINE})
            session.commit()
        _drain(engine, workers, project_id)
        edited = _snapshot(sessions, project_id)
        initial_job_ids = {job.id for job in initial_jobs}
        reruns = [job for job in queue.list(project_id) if job.id not in initial_job_ids]
        rerun_nodes = [edited[str(job.node_id)]["label"] for job in reruns]
        output(f"Edited shot 2 line: {len(rerun_nodes)} reran: {', '.join(rerun_nodes)}")

        # Dirty the same recipe deliberately: every clean transition below must
        # reuse an existing version, without submitting or executing a new job.
        with sessions() as session:
            project = session.get(Project, project_id)
            assert project is not None
            voice = next(
                node
                for node in project.nodes
                if node.kind == NodeKind.VOICEOVER and node.shot_index == 2
            )
            ProjectRepo(session).update_recipe(voice, {})
            session.commit()
        dirty = _snapshot(sessions, project_id)
        jobs_before_cache = queue.list(project_id)
        _drain(engine, workers, project_id)
        cached = _snapshot(sessions, project_id)
        new_job_nodes = {
            job.node_id
            for job in queue.list(project_id)
            if job.id not in {previous.id for previous in jobs_before_cache}
        }
        cache_nodes = []
        for label in rerun_nodes:
            node_id = next(node_id for node_id, state in cached.items() if state["label"] == label)
            state = cached[node_id]
            if (
                dirty[node_id]["status"] == NodeStatus.DIRTY
                and state["status"] == NodeStatus.CLEAN
                and state["version_id"] == edited[node_id]["version_id"]
                and state["version_count"] == edited[node_id]["version_count"]
                and node_id not in new_job_nodes
            ):
                cache_nodes.append(label)
        output(f"Unchanged recipe: {len(cache_nodes)} cache hits: {', '.join(cache_nodes)}")
        version_final = _final_path(cached, root)
        final = root / "demo" / "final.mp4"
        # Copy and replace the convenience export only; version paths remain immutable.
        staging = run_dir / "export.mp4"
        shutil.copyfile(version_final, staging)
        staging.replace(final)
        output(f"Final MP4: {final}")
        return {
            "project_id": project_id,
            "projectid": project_id,
            "data_dir": str(root),
            "database_path": str(database_path),
            "final_path": str(final),
            "final_paths": {
                "export": str(final),
                "initial_version": str(_final_path(initial, root)),
                "current_version": str(version_final),
            },
            "initial_final_path": str(_final_path(initial, root)),
            "version_final_path": str(version_final),
            "shot_count": 3,
            "initial_nodes": initial_nodes,
            "rerun_nodes": rerun_nodes,
            "rerun_labels": rerun_nodes,
            "cache_hit_nodes": cache_nodes,
            "cache_hits": len(cache_nodes),
            "job_count": len(queue.list(project_id)),
        }
    finally:
        database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Media root (default: configured data_dir)")
    args = parser.parse_args(argv)
    local_tools = Path.cwd() / "data" / "tools" / "ffmpeg"
    extension = ".exe" if os.name == "nt" else ""
    if all((local_tools / f"{tool}{extension}").is_file() for tool in ("ffmpeg", "ffprobe")):
        os.environ["PATH"] = str(local_tools) + os.pathsep + os.environ.get("PATH", "")
    try:
        run_demo(args.data_dir)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Offline demo: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
