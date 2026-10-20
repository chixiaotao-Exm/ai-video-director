"""Validated project-scoped tools exposed to the director language model."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.agents.script import update_shot
from avd.assets.library import AssetLibrary
from avd.assets.storage import AssetStorage
from avd.engine.rebuild import JOB_TYPES
from avd.jobs.models import Job, JobStatus
from avd.llm.budget import BudgetExceeded, BudgetGuard, acquire_project_budget
from avd.llm.reservations import reserved_cost
from avd.state.consent import require_usable
from avd.state.models import Node, NodeKind, Project
from avd.state.repo import ProjectRepo

from . import mindmap
from .review import (
    ReviewRequired,
    approve,
    budget_is_confirmed,
    require_review,
    review_status,
)


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class EmptyInput(ToolInput):
    pass


class RebuildInput(ToolInput):
    retry_failed: bool = False


class CharacterInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    reference_images: dict[Literal["front", "side", "half", "full"], str] = Field(
        default_factory=dict
    )
    appearance_prompt: str = Field(min_length=1)
    generate_sheet: bool = False


class VoiceDefaults(ToolInput):
    speed: float = Field(default=1.0, gt=0)
    emotion: str = Field(default="neutral", min_length=1)


class VoiceInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    sample_paths: list[str] = Field(min_length=1)
    engine: str = Field(min_length=1)
    engine_voice_id: str | None = None
    default_params: VoiceDefaults = Field(default_factory=VoiceDefaults)


class ScriptInput(ToolInput):
    brief: str = Field(min_length=1)


class ShotChanges(ToolInput):
    line: str | None = None
    emotion: str | None = Field(default=None, min_length=1)
    visual_prompt: str | None = Field(default=None, min_length=1)
    camera: str | None = Field(default=None, min_length=1)
    duration_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def nonempty(self) -> Self:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Supply at least one non-null shot change")
        return self


class UpdateShotInput(ToolInput):
    shot_index: int = Field(ge=1, strict=True)
    changes: ShotChanges


class NodeInput(ToolInput):
    node_id: str = Field(min_length=1)


class RevertInput(NodeInput):
    version_number: int = Field(ge=1, strict=True)


class WebSearchInput(ToolInput):
    query: str = Field(min_length=2, max_length=2000)


class SeparateInput(ToolInput):
    stems: bool = Field(
        default=False, description="Also split the audio into vocals and accompaniment."
    )
    source: str = Field(
        default="reference",
        max_length=1024,
        description='"reference" for this project\'s reference video, or an uploaded video path.',
    )


class SendItem(ToolInput):
    path: str = Field(min_length=1, max_length=1024, description="A media path under data_dir.")
    caption: str | None = Field(default=None, max_length=500)


class SendMediaInput(ToolInput):
    items: list[SendItem] = Field(min_length=1, max_length=12)


class CheckJobsInput(ToolInput):
    status: Literal["all", "active", "failed", "succeeded"] = "all"
    limit: int = Field(default=15, ge=1, le=50)


class WatchInput(ToolInput):
    job_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Jobs to watch; empty means every queued or running job of this project.",
    )
    instructions: str = Field(
        min_length=1, max_length=1000, description="What you will do once they finish."
    )
    name: str = Field(default="盯任务", min_length=1, max_length=40)


class DelegateInput(ToolInput):
    name: str = Field(min_length=1, max_length=40, description="A short role name, e.g. 调研员.")
    task: str = Field(min_length=1, max_length=4000)


class SubAgentMessageInput(ToolInput):
    agent_id: str = Field(min_length=1, max_length=36)
    message: str = Field(min_length=1, max_length=4000)


class SubAgentRefInput(ToolInput):
    agent_id: str = Field(min_length=1, max_length=36)


class MindMapInput(ToolInput):
    nodes: list[mindmap.MindMapItem] = Field(default_factory=list, max_length=40)
    remove: list[str] = Field(default_factory=list, max_length=40)


class ApproveInput(ToolInput):
    stage: Literal["structure", "script", "frames", "final"]
    user_quote: str = Field(
        min_length=1,
        max_length=500,
        description="The exact words from the user's latest message that approve this stage.",
    )


_TOOLS: dict[str, tuple[type[ToolInput], str]] = {
    "analyze_video": (
        EmptyInput,
        "Queue analysis of the uploaded reference video; return a job ID.",
    ),
    "create_character": (
        CharacterInput,
        "Create an unconsented character from uploads, or queue a fictional sheet with generate_sheet=true.",
    ),
    "create_voice": (VoiceInput, "Create an unconsented voice from uploaded sample paths."),
    "write_script": (ScriptInput, "Queue an original storyboard after human structure approval."),
    "update_shot": (UpdateShotInput, "Edit one shot and dirty only affected downstream nodes."),
    "generate_frames": (EmptyInput, "Queue first frames after human script approval."),
    "generate_videos": (EmptyInput, "Queue shot videos after human frame approval."),
    "generate_voiceover": (EmptyInput, "Queue voiceover after human frame approval."),
    "run_lipsync": (EmptyInput, "Queue lip sync after human frame approval."),
    "build_subtitles": (EmptyInput, "Queue subtitles after human frame approval."),
    "render_final": (EmptyInput, "Queue final assembly after human frame approval."),
    "plan_rebuild": (EmptyInput, "List dirty nodes in dependency order without starting work."),
    "rebuild": (
        RebuildInput,
        "Queue approved incremental work; explicitly retry failed nodes if requested.",
    ),
    "revert": (RevertInput, "Restore a version belonging to this project's node."),
    "list_versions": (NodeInput, "List immutable versions belonging to this project's node."),
    "get_project_status": (EmptyInput, "Read this project's node, review and job progress."),
    "get_budget": (EmptyInput, "Read token usage, spending and costs reserved by pending jobs."),
    "web_search": (
        WebSearchInput,
        "Search the web for current information (trends, references, techniques, model or "
        "tool docs). Returns an answer with source URLs. Use it whenever fresh facts help.",
    ),
    "separate_audio_video": (
        SeparateInput,
        "Split a video into a silent video track and an audio track and save both to the "
        "asset library (媒体素材), e.g. to reuse the music or study the picture alone.",
    ),
    "check_jobs": (
        CheckJobsInput,
        "See this project's background jobs: type, shot, status, attempts, timing, the "
        "error if it failed, and the output files if it finished.",
    ),
    "list_library": (
        EmptyInput,
        "List the asset library: characters, voices (with consent state) and media clips "
        "(e.g. separated video/audio/vocals/accompaniment) with their file paths.",
    ),
    "send_media": (
        SendMediaInput,
        "Send images, video or audio files to the user in the chat (like a messaging app). "
        "Use output paths from check_jobs, list_versions or the asset library.",
    ),
    "assign_watcher": (
        WatchInput,
        "Assign a watcher sub-agent to follow jobs you queued. It reports to you as soon as "
        "they all finish, so you can send the results without the user asking.",
    ),
    "delegate_task": (
        DelegateInput,
        "Hand a task to a worker sub-agent with its own model and read-only tools (web "
        "search, project status, jobs, library). It reports back to you when done.",
    ),
    "message_subagent": (
        SubAgentMessageInput,
        "Send a follow-up message to a worker sub-agent; its answer comes back as a report.",
    ),
    "list_subagents": (EmptyInput, "List this project's sub-agents with status and results."),
    "stop_subagent": (SubAgentRefInput, "Stop a sub-agent that is no longer needed."),
    "update_mindmap": (
        MindMapInput,
        "Add or update nodes in the live mind map the user watches: insights, options, "
        "decisions, open questions and tasks, each linked to the node it follows from "
        "(parent: root, reference, script, shot-N or your own ids). Reuse ids to update "
        "status (open/chosen/rejected/done). Keep labels short; put detail in note.",
    ),
    "approve_checkpoint": (
        ApproveInput,
        "Record the user's approval of a checkpoint (structure, script, frames, final) ONLY "
        "when their latest message clearly approves it; quote their exact words.",
    ),
}

_GENERATORS = {
    "generate_frames": NodeKind.SHOT_FRAME,
    "generate_videos": NodeKind.SHOT_VIDEO,
    "generate_voiceover": NodeKind.VOICEOVER,
    "run_lipsync": NodeKind.LIPSYNC,
    "build_subtitles": NodeKind.SUBTITLES,
    "render_final": NodeKind.FINAL,
}


_MEDIA_KINDS = {
    "image": (".png", ".jpg", ".jpeg", ".webp", ".gif"),
    "video": (".mp4", ".mov", ".webm", ".mkv"),
    "audio": (".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"),
}


def media_kind(path: str) -> str | None:
    lowered = path.lower()
    return next(
        (kind for kind, suffixes in _MEDIA_KINDS.items() if lowered.endswith(suffixes)), None
    )


def _summary(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def project_node(session: Session, project: Project, node_id: str) -> Node:
    node = session.get(Node, node_id)
    if node is None or node.project_id != project.id:
        raise KeyError("Node was not found in this project")
    return node


class DirectorTools:
    def __init__(
        self, runtime: Any, project_id: str, latest_user_message: str | None = None
    ) -> None:
        self.runtime = runtime
        self.project_id = project_id
        self.latest_user_message = latest_user_message
        self._attachments: list[dict[str, str]] = []

    def take_attachments(self) -> list[dict[str, str]]:
        sent, self._attachments = self._attachments, []
        return sent

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [
            {"name": name, "description": description, "input_schema": schema.model_json_schema()}
            for name, (schema, description) in _TOOLS.items()
        ]

    def _project(self, session: Session) -> Project:
        project = session.get(Project, self.project_id)
        if project is None:
            raise KeyError(f"Project {self.project_id} was not found")
        return project

    def _uploaded_path(self, value: str) -> str:
        relative = Path(value)
        windows = PureWindowsPath(value)
        if (
            not value
            or relative.is_absolute()
            or windows.drive
            or windows.root
            or ".." in relative.parts
            or ".." in windows.parts
        ):
            raise ValueError("Asset paths must be relative uploaded media paths")
        root = self.runtime.settings.data_dir.expanduser().resolve()
        uploaded = (root / "uploads").resolve()
        path = (root / relative).resolve()
        if not uploaded.is_relative_to(root) or not path.is_relative_to(uploaded):
            raise ValueError("Asset paths must stay under data_dir/uploads")
        if not path.is_file():
            raise FileNotFoundError("Uploaded asset does not exist")
        return str(path)

    @staticmethod
    def _pending(session: Session, project: Project) -> list[Job]:
        return list(
            session.scalars(
                select(Job).where(
                    Job.project_id == project.id,
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
            ).all()
        )

    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in _TOOLS:
            raise ValueError(f"Unknown director tool: {name}")
        parsed = _TOOLS[name][0].model_validate(args)
        if (
            name == "create_character"
            and isinstance(parsed, CharacterInput)
            and parsed.generate_sheet
        ):
            from avd.assets.sheets import submit_character_sheet

            if parsed.reference_images:
                raise ValueError("Generated fictional sheets accept only a text prompt")
            job_id = submit_character_sheet(
                self.runtime, self.project_id, parsed.name, parsed.appearance_prompt
            )
            return _summary(
                {
                    "job_id": job_id,
                    "message": "Character sheet queued for human review and consent.",
                }
            )
        if isinstance(parsed, WebSearchInput):
            if not self.runtime.settings.web_search_enabled:
                raise ValueError("Web search is disabled in settings")
            with self.runtime.session_factory() as session:
                project = self._project(session)
                try:
                    found = self.runtime.router(session, project).search(
                        parsed.query, project=project
                    )
                finally:
                    session.commit()
            return found.content or "No results."
        if isinstance(parsed, SeparateInput):
            from avd.assets.media import asset_data, separate

            root = self.runtime.settings.data_dir.expanduser().resolve()
            with self.runtime.session_factory() as session:
                project = self._project(session)
                if parsed.source == "reference":
                    if not project.source_video_path:
                        raise ValueError("This project has no reference video yet")
                    relative = project.source_video_path
                else:
                    relative = (
                        Path(self._uploaded_path(parsed.source)).resolve().relative_to(root)
                    ).as_posix()
                created = separate(
                    session,
                    root,
                    relative,
                    name=project.title,
                    project_id=project.id,
                    stems=parsed.stems,
                )
                session.commit()
                return _summary(
                    {
                        "assets": [asset_data(asset) for asset in created],
                        "message": "Saved to the asset library under 媒体素材.",
                    }
                )
        if isinstance(parsed, SendMediaInput):
            root = self.runtime.settings.data_dir.expanduser().resolve()
            for item in parsed.items:
                kind = media_kind(item.path)
                target = (root / item.path).resolve()
                if kind is None or not target.is_relative_to(root) or not target.is_file():
                    raise ValueError(f"Not a media file under data_dir: {item.path}")
                self._attachments.append(
                    {
                        "kind": kind,
                        "path": target.relative_to(root).as_posix(),
                        "caption": item.caption or "",
                    }
                )
            return f"Sent {len(parsed.items)} file(s) to the user."
        if name == "list_library":
            from avd.assets.media import list_assets

            with self.runtime.session_factory() as session:
                library = AssetLibrary(session, AssetStorage(self.runtime.settings.data_dir))
                return _summary(
                    {
                        "characters": [
                            {
                                "id": item.id,
                                "name": item.name,
                                "consent": item.consent_confirmed,
                                "images": item.reference_images,
                            }
                            for item in library.list_characters()
                        ],
                        "voices": [
                            {
                                "id": item.id,
                                "name": item.name,
                                "consent": item.consent_confirmed,
                                "samples": item.sample_audio_paths,
                            }
                            for item in library.list_voices()
                        ],
                        "media": [
                            {"name": item.name, "kind": item.kind, "path": item.path}
                            for item in list_assets(session)
                        ],
                    }
                )
        if isinstance(parsed, CheckJobsInput):
            with self.runtime.session_factory() as session:
                statement = select(Job).where(Job.project_id == self.project_id)
                if parsed.status == "active":
                    statement = statement.where(
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])
                    )
                elif parsed.status == "failed":
                    statement = statement.where(Job.status == JobStatus.FAILED)
                elif parsed.status == "succeeded":
                    statement = statement.where(Job.status == JobStatus.SUCCEEDED)
                rows = session.scalars(
                    statement.order_by(Job.created_at.desc()).limit(parsed.limit)
                ).all()
                node_index = {
                    node.id: node
                    for node in session.scalars(
                        select(Node).where(Node.project_id == self.project_id)
                    )
                }
                report = []
                for job in rows:
                    node = node_index.get(job.node_id or "")
                    outputs: Any = None
                    if node is not None and node.current_version is not None:
                        outputs = node.current_version.output_paths
                    elif isinstance(job.result, dict):
                        outputs = {
                            key: value
                            for key, value in job.result.items()
                            if isinstance(value, str) and media_kind(value)
                        } or None
                    report.append(
                        {
                            "id": job.id,
                            "type": job.job_type,
                            "lane": job.lane.value,
                            "kind": node.kind.value if node else None,
                            "shot": node.shot_index if node else None,
                            "status": job.status.value,
                            "attempts": f"{job.attempts}/{job.max_attempts}",
                            "created_at": job.created_at,
                            "started_at": job.started_at,
                            "finished_at": job.finished_at,
                            "error": (job.error or "").strip().splitlines()[-1:] or None,
                            "outputs": outputs,
                        }
                    )
            return _summary(report)
        if isinstance(
            parsed, WatchInput | DelegateInput | SubAgentMessageInput | SubAgentRefInput
        ) or (name == "list_subagents"):
            return self._subagents(name, parsed)
        if isinstance(parsed, MindMapInput):
            with self.runtime.session_factory() as session:
                project = self._project(session)
                changed = mindmap.update(session, project, parsed.nodes, parsed.remove)
                session.commit()
            return _summary(changed)
        if isinstance(parsed, ApproveInput):
            quote = " ".join(parsed.user_quote.split())
            latest = " ".join((self.latest_user_message or "").split())
            if not quote or quote not in latest:
                raise ReviewRequired(
                    "The quote is not in the user's latest message; ask the user to approve."
                )
            with self.runtime.session_factory() as session:
                project = self._project(session)
                approval = approve(
                    session, project, parsed.stage, approved_by=f"chat: {quote}"[:255]
                )
                session.commit()
            return _summary(approval)
        if name in _GENERATORS or name == "rebuild":
            return self._generate(
                name, retry_failed=isinstance(parsed, RebuildInput) and parsed.retry_failed
            )
        with self.runtime.session_factory() as session:
            project = self._project(session)
            repo = ProjectRepo(session)
            if name in {"analyze_video", "write_script"}:
                result = self._authoring(session, project, name, parsed)
            elif name == "create_character":
                assert isinstance(parsed, CharacterInput)
                paths: dict[str, str] = {
                    view: self._uploaded_path(path)
                    for view, path in parsed.reference_images.items()
                }
                asset = AssetLibrary(
                    session, AssetStorage(self.runtime.settings.data_dir)
                ).create_character(parsed.name, paths, parsed.appearance_prompt)
                result = {"character_id": asset.id, "consent_required": True}
            elif name == "create_voice":
                assert isinstance(parsed, VoiceInput)
                samples = [self._uploaded_path(path) for path in parsed.sample_paths]
                voice = AssetLibrary(
                    session, AssetStorage(self.runtime.settings.data_dir)
                ).create_voice(
                    parsed.name,
                    samples,
                    parsed.engine,
                    parsed.engine_voice_id,
                    default_params=parsed.default_params.model_dump(),
                )
                result = {"voice_id": voice.id, "consent_required": True}
            elif name == "update_shot":
                assert isinstance(parsed, UpdateShotInput)
                nodes = update_shot(
                    project,
                    parsed.shot_index,
                    parsed.changes.model_dump(exclude_unset=True),
                    repo=repo,
                )
                result = {
                    "updated_nodes": [node.id for node in nodes],
                    "message": "Shot updated; review the revised script before regeneration.",
                }
            elif name in {"revert", "list_versions"}:
                assert isinstance(parsed, NodeInput)
                node = project_node(session, project, parsed.node_id)
                if name == "revert":
                    assert isinstance(parsed, RevertInput)
                    version = repo.revert(node, parsed.version_number)
                    result = {
                        "node_id": node.id,
                        "version": version.number,
                        "message": "Version restored; downstream nodes require rebuild.",
                    }
                else:
                    result = [
                        {
                            "number": item.number,
                            "id": item.id,
                            "outputs": item.output_paths,
                            "note": item.note,
                            "cost": item.cost,
                        }
                        for item in repo.list_versions(node)
                    ]
            elif name == "plan_rebuild":
                result = [
                    {"id": node.id, "kind": node.kind.value, "shot_index": node.shot_index}
                    for node in repo.plan_rebuild(project)
                ]
            elif name == "get_project_status":
                result = {
                    "project_id": project.id,
                    "title": project.title,
                    "source_video": project.source_video_path,
                    "analysis": project.analysis_result,
                    "character_id": project.character_id,
                    "voice_id": project.voice_id,
                    "nodes": [
                        {
                            "id": node.id,
                            "kind": node.kind.value,
                            "shot_index": node.shot_index,
                            "status": node.status.value,
                            "version_id": node.current_version_id,
                        }
                        for node in project.nodes
                    ],
                    "reviews": review_status(session, project),
                    "jobs": [
                        {
                            "id": job.id,
                            "type": job.job_type,
                            "status": job.status.value,
                            "error": job.error,
                        }
                        for job in session.scalars(
                            select(Job)
                            .where(Job.project_id == project.id)
                            .order_by(Job.created_at.desc())
                            .limit(20)
                        ).all()
                    ],
                }
            else:
                reserved = reserved_cost(session, project.id)
                result = {
                    "budget_limit": project.budget_limit,
                    "spent": project.spent,
                    "reserved": reserved,
                    "remaining": None
                    if project.budget_limit is None
                    else project.budget_limit - project.spent - reserved,
                    "confirm_threshold": self.runtime.settings.confirm_threshold,
                }
            session.commit()
            return _summary(result)

    def _subagents(self, name: str, parsed: ToolInput) -> str:
        from . import subagents

        with self.runtime.session_factory() as session:
            project = self._project(session)
            if isinstance(parsed, WatchInput):
                agent = subagents.create_watcher(
                    session,
                    project,
                    name=parsed.name,
                    instructions=parsed.instructions,
                    job_ids=parsed.job_ids or None,
                )
                session.commit()
                return _summary(
                    {"agent_id": agent.id, "watching": len(agent.job_ids), "status": "watching"}
                )
            if isinstance(parsed, DelegateInput):
                agent, job_id = subagents.create_worker(
                    self.runtime, session, project, name=parsed.name, task=parsed.task
                )
                session.commit()
                return _summary({"agent_id": agent.id, "job_id": job_id, "status": "working"})
            if name == "list_subagents":
                agents = session.scalars(
                    select(subagents.SubAgent)
                    .where(subagents.SubAgent.project_id == project.id)
                    .order_by(subagents.SubAgent.created_at.desc())
                ).all()
                return subagents.summary(list(agents))
            assert isinstance(parsed, SubAgentMessageInput | SubAgentRefInput)
            agent = session.get(subagents.SubAgent, parsed.agent_id)
            if agent is None or agent.project_id != project.id:
                raise KeyError("Sub-agent not found in this project")
            if isinstance(parsed, SubAgentRefInput):
                agent.status = "stopped"
                session.commit()
                return _summary({"agent_id": agent.id, "status": "stopped"})
            if agent.kind != "worker":
                raise ValueError("Only worker sub-agents can be messaged")
            if agent.status == "working":
                raise ValueError("That sub-agent is still working; wait for its report")
            job_id = subagents.submit_worker_turn(self.runtime, session, agent, parsed.message)
            session.commit()
            return _summary({"agent_id": agent.id, "job_id": job_id, "status": "working"})

    def _authoring(self, session: Session, project: Project, name: str, parsed: ToolInput) -> Any:
        acquire_project_budget(project)
        pending = self._pending(session, project)
        existing = next((job for job in pending if job.job_type == name), None)
        if existing is not None:
            return {"job_id": existing.id, "status": existing.status.value}
        payload: dict[str, Any] = {"project_id": project.id}
        if name == "analyze_video":
            if not project.source_video_path:
                raise ValueError("Upload a reference video before analysis")
            role = "analysis"
        else:
            assert isinstance(parsed, ScriptInput)
            require_review(session, project, "structure")
            if project.character is None or project.voice is None:
                raise ValueError("Attach a consented character and voice before writing a script")
            require_usable(project.character)
            require_usable(project.voice)
            payload["brief"] = parsed.brief
            role = "script"
        estimate = self.runtime.settings.job_estimated_costs.get(name, 0.0)
        reserved = reserved_cost(session, project.id)
        confirmed = budget_is_confirmed(session, project, estimate + reserved)
        BudgetGuard(self.runtime.settings.confirm_threshold).check(
            project, estimate, confirmed=confirmed
        )
        if (
            project.budget_limit is not None
            and project.spent + reserved + estimate > project.budget_limit
        ):
            raise BudgetExceeded("Pending work plus this job exceeds the project budget")
        payload.update(estimated_cost=estimate, confirmed=confirmed)
        job_id = self.runtime.queue.submit(
            "api",
            name,
            payload,
            project_id=project.id,
            model_key=self.runtime.settings.llm_route(role).model,
            session=session,
        )
        return {"job_id": job_id, "status": "queued"}

    def _generate(self, name: str, *, retry_failed: bool = False) -> str:
        waiting = False
        gpu_note = ""
        with self.runtime.session_factory() as session:
            project = self._project(session)
            require_review(session, project, "script")
            if name == "generate_frames":
                kinds = {NodeKind.SHOT_FRAME}
            elif name == "rebuild":
                try:
                    require_review(session, project, "frames")
                except ReviewRequired:
                    kinds = {NodeKind.SHOT_FRAME}
                    waiting = True
                else:
                    kinds = set(JOB_TYPES)
            else:
                require_review(session, project, "frames")
                kinds = {_GENERATORS[name]}
            plan = ProjectRepo(session).plan_rebuild(project)
            gpu_kinds = {NodeKind.SHOT_VIDEO, NodeKind.VOICEOVER, NodeKind.LIPSYNC}
            if any(node.kind in kinds & gpu_kinds for node in plan):
                from avd.backends.comfyui import comfyui_status

                profile = self.runtime.settings.comfyui_profile
                ready, detail = comfyui_status(profile.base_url)
                if not ready:
                    if kinds - gpu_kinds and name == "rebuild":
                        kinds = kinds - gpu_kinds - {NodeKind.SUBTITLES, NodeKind.FINAL}
                        gpu_note = (
                            f" ComfyUI is not reachable at {profile.base_url} ({detail}); "
                            "video, voice, lip-sync and final steps were not queued."
                        )
                    else:
                        raise ValueError(
                            "ComfyUI is not reachable at "
                            f"{profile.base_url} ({detail}). Video, voice and lip-sync "
                            "need a running ComfyUI on a GPU server; nothing was queued."
                        )
            if retry_failed:
                plan.extend(node for node in project.nodes if node.status == "failed")
            estimate = sum(
                self.runtime.engine.estimate_cost(node) for node in plan if node.kind in kinds
            )
            reserved = reserved_cost(session, project.id)
            confirmed = budget_is_confirmed(session, project, estimate + reserved)
            BudgetGuard(self.runtime.settings.confirm_threshold).check(
                project, estimate, confirmed=confirmed
            )
            if (
                project.budget_limit is not None
                and project.spent + reserved + estimate > project.budget_limit
            ):
                raise BudgetExceeded("Pending work plus this batch exceeds the project budget")
        schedule = self.runtime.engine.retry_failed if retry_failed else self.runtime.engine.rebuild
        job_ids = schedule(self.project_id, kinds=kinds, confirmed=confirmed)
        message = (
            "First frames queued; human frame approval is required before further generation."
            if waiting
            else (
                "Generation jobs queued."
                if job_ids
                else "No new ready work; inspect project status."
            )
        )
        return _summary({"job_ids": job_ids, "message": message + gpu_note})


__all__ = ["DirectorTools", "project_node"]
