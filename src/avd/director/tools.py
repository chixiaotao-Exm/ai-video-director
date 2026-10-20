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
from avd.llm.budget import BudgetExceeded, BudgetGuard
from avd.state.consent import require_usable
from avd.state.models import Node, NodeKind, Project
from avd.state.repo import ProjectRepo

from .review import ReviewRequired, budget_is_confirmed, require_review, review_status


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class EmptyInput(ToolInput):
    pass


class CharacterInput(ToolInput):
    name: str = Field(min_length=1, max_length=255)
    reference_images: dict[Literal["front", "side", "half", "full"], str]
    appearance_prompt: str = Field(min_length=1)


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
        if not self.model_fields_set or any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Supply at least one non-null shot change")
        return self


class UpdateShotInput(ToolInput):
    shot_index: int = Field(ge=1, strict=True)
    changes: ShotChanges


class NodeInput(ToolInput):
    node_id: str = Field(min_length=1)


class RevertInput(NodeInput):
    version_number: int = Field(ge=1, strict=True)


_TOOLS: dict[str, tuple[type[ToolInput], str]] = {
    "analyze_video": (EmptyInput, "Queue analysis of the uploaded reference video; return a job ID."),
    "create_character": (CharacterInput, "Create an unconsented character from uploaded image paths."),
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
    "rebuild": (EmptyInput, "Queue approved incremental work, pausing for first-frame review."),
    "revert": (RevertInput, "Restore a version belonging to this project's node."),
    "list_versions": (NodeInput, "List immutable versions belonging to this project's node."),
    "get_project_status": (EmptyInput, "Read this project's node, review and job progress."),
    "get_budget": (EmptyInput, "Read budget, spending and costs reserved by pending jobs."),
}

_GENERATORS = {
    "generate_frames": NodeKind.SHOT_FRAME, "generate_videos": NodeKind.SHOT_VIDEO,
    "generate_voiceover": NodeKind.VOICEOVER, "run_lipsync": NodeKind.LIPSYNC,
    "build_subtitles": NodeKind.SUBTITLES, "render_final": NodeKind.FINAL,
}


def _summary(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def project_node(session: Session, project: Project, node_id: str) -> Node:
    node = session.get(Node, node_id)
    if node is None or node.project_id != project.id:
        raise KeyError("Node was not found in this project")
    return node


class DirectorTools:
    def __init__(self, runtime: Any, project_id: str) -> None:
        self.runtime = runtime
        self.project_id = project_id

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        return [{"name": name, "description": description,
                 "input_schema": schema.model_json_schema()}
                for name, (schema, description) in _TOOLS.items()]

    def _project(self, session: Session) -> Project:
        project = session.get(Project, self.project_id)
        if project is None:
            raise KeyError(f"Project {self.project_id} was not found")
        return project

    def _uploaded_path(self, value: str) -> str:
        relative = Path(value)
        windows = PureWindowsPath(value)
        if (not value or relative.is_absolute() or windows.drive or windows.root
                or ".." in relative.parts or ".." in windows.parts):
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
        return list(session.scalars(select(Job).where(
            Job.project_id == project.id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )).all())

    def execute(self, name: str, args: dict[str, Any]) -> str:
        if name not in _TOOLS:
            raise ValueError(f"Unknown director tool: {name}")
        parsed = _TOOLS[name][0].model_validate(args)
        if name in _GENERATORS or name == "rebuild":
            return self._generate(name)
        with self.runtime.session_factory() as session:
            project = self._project(session)
            repo = ProjectRepo(session)
            if name in {"analyze_video", "write_script"}:
                result = self._authoring(session, project, name, parsed)
            elif name == "create_character":
                assert isinstance(parsed, CharacterInput)
                paths = {view: self._uploaded_path(path) for view, path in parsed.reference_images.items()}
                asset = AssetLibrary(session, AssetStorage(self.runtime.settings.data_dir)).create_character(
                    parsed.name, paths, parsed.appearance_prompt
                )
                result = {"character_id": asset.id, "consent_required": True}
            elif name == "create_voice":
                assert isinstance(parsed, VoiceInput)
                paths = [self._uploaded_path(path) for path in parsed.sample_paths]
                voice = AssetLibrary(session, AssetStorage(self.runtime.settings.data_dir)).create_voice(
                    parsed.name, paths, parsed.engine, parsed.engine_voice_id,
                    default_params=parsed.default_params.model_dump(),
                )
                result = {"voice_id": voice.id, "consent_required": True}
            elif name == "update_shot":
                assert isinstance(parsed, UpdateShotInput)
                nodes = update_shot(project, parsed.shot_index,
                                    parsed.changes.model_dump(exclude_unset=True), repo=repo)
                result = {"updated_nodes": [node.id for node in nodes],
                          "message": "Shot updated; review the revised script before regeneration."}
            elif name in {"revert", "list_versions"}:
                assert isinstance(parsed, NodeInput)
                node = project_node(session, project, parsed.node_id)
                if name == "revert":
                    assert isinstance(parsed, RevertInput)
                    version = repo.revert(node, parsed.version_number)
                    result = {"node_id": node.id, "version": version.number,
                              "message": "Version restored; downstream nodes require rebuild."}
                else:
                    result = [{"number": item.number, "id": item.id, "outputs": item.output_paths,
                               "note": item.note, "cost": item.cost}
                              for item in repo.list_versions(node)]
            elif name == "plan_rebuild":
                result = [{"id": node.id, "kind": node.kind.value, "shot_index": node.shot_index}
                          for node in repo.plan_rebuild(project)]
            elif name == "get_project_status":
                result = {
                    "project_id": project.id, "title": project.title,
                    "nodes": [{"id": node.id, "kind": node.kind.value, "shot_index": node.shot_index,
                               "status": node.status.value, "version_id": node.current_version_id}
                              for node in project.nodes],
                    "reviews": review_status(session, project),
                    "jobs": [{"id": job.id, "type": job.job_type, "status": job.status.value,
                              "error": job.error} for job in session.scalars(select(Job).where(
                                  Job.project_id == project.id
                              ).order_by(Job.created_at.desc()).limit(20)).all()],
                }
            else:
                reserved = sum(float(job.payload.get("estimated_cost", 0))
                               for job in self._pending(session, project))
                result = {"budget_limit": project.budget_limit, "spent": project.spent,
                          "reserved": reserved, "remaining": None if project.budget_limit is None
                          else project.budget_limit - project.spent - reserved,
                          "confirm_threshold": self.runtime.settings.confirm_threshold}
            session.commit()
            return _summary(result)

    def _authoring(self, session: Session, project: Project, name: str, parsed: ToolInput) -> Any:
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
        reserved = sum(float(job.payload.get("estimated_cost", 0)) for job in pending)
        confirmed = budget_is_confirmed(session, project, estimate + reserved)
        BudgetGuard(self.runtime.settings.confirm_threshold).check(project, estimate, confirmed=confirmed)
        if project.budget_limit is not None and project.spent + reserved + estimate > project.budget_limit:
            raise BudgetExceeded("Pending work plus this job exceeds the project budget")
        payload.update(estimated_cost=estimate, confirmed=confirmed)
        job_id = self.runtime.queue.submit(
            "api", name, payload, project_id=project.id,
            model_key=self.runtime.settings.llm_route(role).model, session=session,
        )
        return {"job_id": job_id, "status": "queued"}

    def _generate(self, name: str) -> str:
        waiting = False
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
            estimate = sum(self.runtime.engine.estimate_cost(node) for node in plan if node.kind in kinds)
            reserved = sum(float(job.payload.get("estimated_cost", 0))
                           for job in self._pending(session, project))
            confirmed = budget_is_confirmed(session, project, estimate + reserved)
            BudgetGuard(self.runtime.settings.confirm_threshold).check(project, estimate, confirmed=confirmed)
            if project.budget_limit is not None and project.spent + reserved + estimate > project.budget_limit:
                raise BudgetExceeded("Pending work plus this batch exceeds the project budget")
        job_ids = self.runtime.engine.rebuild(self.project_id, kinds=kinds, confirmed=confirmed)
        message = "First frames queued; human frame approval is required before further generation." if waiting else (
            "Generation jobs queued." if job_ids else "No new ready work; inspect project status."
        )
        return _summary({"job_ids": job_ids, "message": message})


__all__ = ["DirectorTools", "project_node"]
