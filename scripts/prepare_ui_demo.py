"""Prepare reproducible, fully offline real-media data for browser screenshots.

Run from the repository: ``python scripts/prepare_ui_demo.py``. Each invocation
creates a fresh immutable demo run; ui-manifest.json selects the latest run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import select

from avd.agents.analysis import StructureTemplate
from avd.config import Settings
from avd.demo import run_demo
from avd.director.models import ChatMessage
from avd.director.review import STAGES, approve, approve_budget
from avd.llm.ledger import CostEntry
from avd.runtime import Runtime
from avd.settings_store import SettingsStore
from avd.state.models import NodeKind, Project, Version
from avd.state.repo import ProjectRepo


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").is_file() and (parent / "pyproject.toml").is_file():
            return parent
    raise RuntimeError("Run this script from the ai-video-director repository")


@contextmanager
def _offline() -> Iterator[None]:
    def blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("UI demo preparation is offline; network access is forbidden")

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create = socket.create_connection
    socket.socket.connect = blocked  # type: ignore[assignment]
    socket.socket.connect_ex = blocked  # type: ignore[assignment]
    socket.create_connection = blocked  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create


def _template(duration_s: float) -> StructureTemplate:
    return StructureTemplate.model_validate(
        {
            "total_duration_s": 3 * duration_s,
            "hook": "用第一张标题卡提出主题，让观众立即理解三镜头的演示结构。",
            "shots": [
                {
                    "duration_s": duration_s,
                    "shot_size": "全屏标题卡",
                    "camera_move": "缓慢推进",
                    "visual_description": visual,
                    "spoken_line_purpose": purpose,
                }
                for visual, purpose in (
                    ("深蓝色的抽象标题卡，白色文字介绍项目。", "介绍主题与观看目标。"),
                    ("暖棕色的标题卡，保持一致的字体与留白。", "说明只改一句台词的操作。"),
                    ("深绿色的标题卡，以简短结语完成叙事。", "总结版本复用与局部重建的结果。"),
                )
            ],
            "pacing_notes": "三个等长镜头依次推进；前后呼应，保持清晰、短促的演示节奏。",
            "music_style": "无配乐，使用本地合成提示音展示音轨。",
            "voice_style": "无真人声音；合成正弦提示音仅用于验证音频流程。",
        }
    )


def _seed_review_data(report: dict[str, Any], root: Path, duration_s: float) -> dict[str, Any]:
    database = Path(report["database_path"]).resolve()
    if not database.is_relative_to(root):
        raise ValueError("Demo database must remain under its data directory")
    settings = Settings(
        _env_file=None,
        data_dir=root,
        database_url=f"sqlite:///{database.as_posix()}",
        anthropic_api_key=None,
        openai_api_key=None,
        gemini_api_key=None,
    )
    # Only a new fixture gets a sanitized overlay. Existing browser settings
    # may contain user-entered credentials and must survive every rerun intact.
    # Screenshots should use a fresh --data-dir when an older fixture was edited.
    store = SettingsStore(settings)
    if not store.path.exists():
        store.update(
            {
                "anthropic_api_key": None,
                "openai_api_key": None,
                "gemini_api_key": None,
                "anthropic_base_url": None,
                "openai_base_url": None,
                "gemini_base_url": None,
                "default_budget_limit": 0.0,
                "job_estimated_costs": {},
            }
        )
    runtime = Runtime(settings)
    try:
        with runtime.session_factory() as session:
            project = session.get(Project, report["project_id"])
            if project is None:
                raise RuntimeError("Offline demo did not publish its project")
            previous_versions = {
                version.id: (version.recipe_hash, dict(version.output_paths))
                for version in session.scalars(
                    select(Version).where(Version.node_id.in_([node.id for node in project.nodes]))
                )
            }
            project.title = "三镜头创作 · 离线演示"
            if project.character is not None:
                project.character.name = "演示人物 · 抽象标题卡"
            if project.voice is not None:
                project.voice.name = "演示声音 · 合成提示音"

            uploads = root / "uploads"
            uploads.mkdir(exist_ok=True)
            source = uploads / f"ui-demo-{uuid.uuid4().hex}.mp4"
            with (
                Path(report["version_final_path"]).open("rb") as incoming,
                source.open("xb") as out,
            ):
                shutil.copyfileobj(incoming, out)
            project.source_video_path = source.relative_to(root).as_posix()
            template = _template(duration_s).model_dump(mode="json")
            project.analysis_result = template
            analysis_root = root / "projects" / project.id / "analysis" / uuid.uuid4().hex
            analysis_root.mkdir(parents=True, exist_ok=False)
            template_path = analysis_root / "template.json"
            with template_path.open("x", encoding="utf-8") as stream:
                json.dump(template, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            with source.open("rb") as stream:
                source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            repo = ProjectRepo(session)
            analysis = repo.add_node(
                project,
                NodeKind.ANALYSIS,
                recipe={
                    "source_video_path": project.source_video_path,
                    "source_sha256": source_hash,
                    "provider": "offline",
                    "model": "manual-ui-demo-template",
                    "template": template,
                    "media_info": {
                        "duration_s": 3 * duration_s,
                        "width": 640,
                        "height": 360,
                        "fps": 30,
                        "has_audio": True,
                    },
                },
            )
            repo.record_version(
                analysis,
                {"template": template_path.relative_to(root).as_posix()},
                note="离线 UI 演示：人工编写的合成视频结构模板，无模型调用。",
            )
            for stage in STAGES:
                approve(session, project, stage, "离线演示检查点（合成内容）")
            approve_budget(session, project, 0, "离线演示：费用为零")

            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": "请用三个镜头演示：修改一句台词后，只重建受影响的部分。",
                },
                {
                    "role": "assistant",
                    "content": "这是离线演示对话。三个镜头已使用本地 FFmpeg 图卡和提示音完成生成。",
                    "tool_calls": [
                        {"id": "ui-demo-status", "name": "get_project_status", "arguments": {}}
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "ui-demo-status",
                    "name": "get_project_status",
                    "content": json.dumps(
                        {
                            "summary": "三个镜头与成片均已完成，检查点已确认。",
                            "jobs": report["job_count"],
                        },
                        ensure_ascii=False,
                    ),
                },
                {"role": "user", "content": "把第二个镜头的台词改成更强调版本复用的表达。"},
                {
                    "role": "assistant",
                    "content": "演示已完成这次局部修改：只重建镜头 2 的配音、口型、字幕以及最终成片。镜头 1 和 3 保留原版本，再次执行相同配置得到 4 次缓存命中。可在“镜头”和“成片”页查看版本。",
                },
            ]
            session.add_all(ChatMessage(project_id=project.id, message=value) for value in messages)
            if (
                session.scalar(select(CostEntry.id).where(CostEntry.project_id == project.id))
                is None
            ):
                session.add(
                    CostEntry(
                        project_id=project.id,
                        role="offline_demo",
                        provider="offline",
                        model="ffmpeg-local-media",
                        input_tokens=0,
                        output_tokens=0,
                        est_cost=0,
                    )
                )
            session.flush()
            for identifier, original in previous_versions.items():
                current = session.get(Version, identifier)
                if current is None or (current.recipe_hash, dict(current.output_paths)) != original:
                    raise RuntimeError("Preparing screenshots must not change existing versions")
            session.commit()
        return {
            "data_dir": str(root),
            "database_url": settings.database_url,
            "project_id": report["project_id"],
            "final_path": report["version_final_path"],
            "offline": True,
            "shot_count": report["shot_count"],
            "job_count": report["job_count"],
            "cache_hits": report["cache_hits"],
            "note": "Synthetic offline UI fixture; no human identity, model service, or paid call.",
        }
    finally:
        runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Fixture directory inside this repository")
    parser.add_argument("--duration", type=float, default=2.0, help="Seconds per demo shot")
    args = parser.parse_args(argv)
    repo = _repo_root()
    root = (args.data_dir or repo / "data" / "ui-demo").expanduser().resolve()
    if not root.is_relative_to(repo) or root == repo:
        parser.error("The UI demo must live inside this repository")
    tools = repo / "data" / "tools" / "ffmpeg"
    suffix = ".exe" if os.name == "nt" else ""
    if all((tools / f"{name}{suffix}").is_file() for name in ("ffmpeg", "ffprobe")):
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ.get("PATH", "")
    with _offline():
        report = run_demo(root, duration_s=args.duration)
        manifest = _seed_review_data(report, root, args.duration)
    path = root / "ui-manifest.json"
    temporary = root / f".ui-manifest-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"UI manifest: {path}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
