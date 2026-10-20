"""Manually analyze one video in data_dir; this can make a paid Gemini call."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
from pathlib import Path

from avd.agents.analysis import AnalysisPipeline
from avd.config import Settings
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.repo import ProjectRepo


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="Path to a video already inside AVD_DATA_DIR")
    parser.add_argument("--budget", type=float, default=1.0)
    args = parser.parse_args()
    settings = Settings()
    if not args.video:
        print("Skipped: provide --video pointing to a video inside AVD_DATA_DIR.")
        return 0
    route = settings.llm_route("analysis")
    if route.provider != "gemini" or not route.model or not settings.gemini_api_key:
        print("Skipped: configure a Gemini analysis route/model and AVD_GEMINI_API_KEY.")
        return 0
    for dependency in ("scenedetect", "faster_whisper", "google.genai"):
        try:
            available = importlib.util.find_spec(dependency) is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            print(
                f"Skipped: optional package {dependency!r} is missing; install avd[analysis,llm]."
            )
            return 0
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("Skipped: put ffmpeg and ffprobe on PATH.")
        return 0
    root = settings.data_dir.expanduser().resolve()
    source = Path(args.video).expanduser().resolve()
    if not source.is_file() or not source.is_relative_to(root):
        print("Skipped: --video must be an existing file inside AVD_DATA_DIR.")
        return 0
    engine = create_engine(settings.database_url)
    init_db(engine)
    try:
        with create_session_factory(engine)() as session:
            repo = ProjectRepo(session)
            project = repo.create_project(
                "Analysis manual smoke",
                source_video_path=source.relative_to(root).as_posix(),
                budget_limit=args.budget,
            )
            try:
                template = AnalysisPipeline(repo, settings=settings).analyze_video(project)
            finally:
                # Completed billed calls must remain in the ledger even if a
                # subsequent step fails; old analysis versions remain intact.
                session.commit()
            print(template.model_dump_json(indent=2))
            print(f"project={project.id}; estimated spend=${project.spent:.6f}")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
