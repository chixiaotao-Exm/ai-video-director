"""Shared project operations for HTTP routes and the performance director."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from avd.director.review import ReviewRequired, budget_is_confirmed, require_review
from avd.jobs.models import Job, JobStatus
from avd.llm.budget import BudgetExceeded, BudgetGuard
from avd.llm.reservations import reserved_cost
from avd.state.models import NodeKind, NodeStatus, Project
from avd.state.repo import ProjectRepo

from .graph import configure_performance, file_hash
from .models import PerformanceConfig, PerformanceMixConfig, PreparedPerformance
from .motion import MotionStage, motion_workflow_readiness
from .planning import SegmentPlanRequest, plan_performance_interval
from .probe_state import restore_voice_probe
from .separation import separation_readiness
from .voice import SingingVoiceModelVersion, VoiceModelReadiness, voice_model_readiness
from .workflow_spec import MotionWorkflowSpec

if TYPE_CHECKING:
    from avd.runtime import Runtime


class PreparationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_video_path: str | None = None
    start_s: float | None = Field(default=None, ge=0)
    duration_s: float | None = Field(default=None, gt=0)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)


class PerformanceConfigPatch(BaseModel):
    """Omitted fields retain the saved configuration; optional selections may be cleared."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    mode: Literal["performance_remix"] | None = None
    preparation: PreparationPatch | None = None
    character_id: str | None = None
    voice_id: str | None = None
    scene_prompt: str | None = None
    voice_model_version_id: str | None = None
    voice_parameters: dict[str, Any] | None = None
    mix: PerformanceMixConfig | None = None
    lipsync: bool | None = None
    motion_workflow: MotionWorkflowSpec | None = None
    motion_parameters: dict[str, Any] | None = None
    lipsync_workflow: MotionWorkflowSpec | None = None
    lipsync_parameters: dict[str, Any] | None = None


class PerformanceUnavailable(ReviewRequired):
    def __init__(self, message: str, readiness: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail: str | dict[str, Any] = (
            {"message": message, "readiness": readiness} if readiness is not None else message
        )


def _merge(saved: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(saved))
    for key, value in patch.items():
        previous = result.get(key)
        result[key] = (
            _merge(previous, value)
            if key
            not in {
                "motion_workflow",
                "lipsync_workflow",
                "motion_parameters",
                "lipsync_parameters",
            }
            and isinstance(previous, dict)
            and isinstance(value, dict)
            else deepcopy(value)
        )
    return result


class PerformanceService:
    def __init__(
        self,
        runtime: Runtime,
        *,
        motion_readiness: Callable[..., dict[str, Any]] | None = None,
        voice_readiness: Callable[..., Any] | None = None,
        separation_check: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.runtime = runtime
        self.motion_readiness = motion_readiness or motion_workflow_readiness
        self.voice_readiness = voice_readiness or voice_model_readiness
        self.separation_readiness = separation_check or separation_readiness

    @staticmethod
    def project(session: Session, project_id: str) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise KeyError("Project not found")
        if project.mode != "performance_remix":
            raise PerformanceUnavailable("This project is not in performance_remix mode")
        return project

    def get_config(self, project_id: str) -> dict[str, Any]:
        with self.runtime.session_factory() as session:
            project = self.project(session, project_id)
            return {
                "config": project.performance_config,
                "nodes": [
                    {
                        "id": n.id,
                        "kind": n.kind.value,
                        "status": n.status.value,
                        "current_version_id": n.current_version_id,
                        "depends_on": [p.id for p in n.depends_on],
                    }
                    for n in project.nodes
                ],
            }

    def configure(
        self, project_id: str, patch: PerformanceConfigPatch | Mapping[str, Any]
    ) -> dict[str, Any]:
        changes = PerformanceConfigPatch.model_validate(patch).model_dump(
            mode="json", exclude_unset=True
        )
        with self.runtime.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            project = self.project(session, project_id)
            active = session.scalar(
                select(Job.id)
                .where(
                    Job.project_id == project_id,
                    Job.node_id.is_not(None),
                    Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                )
                .limit(1)
            )
            if active:
                raise PerformanceUnavailable(
                    "Wait for or cancel active jobs before changing inputs"
                )
            if not changes and project.performance_config:
                return self.get_config(project_id)
            saved = deepcopy(project.performance_config or {})
            previous_id = saved.get("voice_model_version_id")
            selected_id = changes.get("voice_model_version_id", previous_id)
            if selected_id != previous_id:
                previous = (
                    session.get(SingingVoiceModelVersion, previous_id) if previous_id else None
                )
                selected = (
                    session.get(SingingVoiceModelVersion, selected_id) if selected_id else None
                )
                if (previous.engine if previous else None) != (
                    selected.engine if selected else None
                ):
                    saved["voice_parameters"] = None
            config = PerformanceConfig.model_validate(_merge(saved, changes))
            preserved: set[MotionStage] = set()
            if "motion_workflow" not in changes:
                preserved.add("motion_video")
            if "lipsync_workflow" not in changes:
                preserved.add("performance_lipsync")
            configure_performance(
                session,
                project,
                config,
                self.runtime.settings.data_dir,
                comfyui_profile=self.runtime.settings.comfyui_profile.model_dump(mode="json"),
                preserve_workflows=preserved,
                preserve_separation="preparation" not in changes,
                demucs_config=self.runtime.settings.demucs_config,
            )
            session.commit()
        return self.get_config(project_id)

    def readiness(self, project_id: str) -> dict[str, Any]:
        runtime = self.runtime
        with runtime.session_factory() as session:
            project = self.project(session, project_id)
            config = project.performance_config
            source_reasons: list[str] = []
            if not config:
                source_reasons.append(
                    "Configure a source video, interval, character, voice and scene"
                )
            else:
                try:
                    digest = file_hash(
                        runtime.settings.data_dir, config["preparation"]["source_video_path"]
                    )
                    prepared = next(
                        n for n in project.nodes if n.kind == NodeKind.PERFORMANCE_PREPARE
                    )
                    if digest != prepared.recipe.get("source_sha256"):
                        source_reasons.append(
                            "Source content changed; save the configuration again"
                        )
                except (OSError, ValueError, KeyError, StopIteration) as exc:
                    source_reasons.append(str(exc) or "Source preparation is not configured")
            stems = next((n for n in project.nodes if n.kind == NodeKind.VOCAL_SEPARATION), None)
            separation_state = self.separation_readiness(
                session, runtime.settings, stems.recipe if stems is not None else {}
            )
            # Keep engine-specific voice readiness delegated to its adapter.
            voice_state = self.voice_readiness(
                session,
                runtime.settings.data_dir,
                str((config or {}).get("voice_model_version_id") or ""),
                installation=runtime.settings.rvc_installation,
                seed_installation=runtime.settings.seed_vc_installation,
            ).model_dump(mode="json")
            voice_state = restore_voice_probe(
                session,
                VoiceModelReadiness.model_validate(voice_state),
                installation=runtime.settings.rvc_installation,
                seed_installation=runtime.settings.seed_vc_installation,
                parameters=(config or {}).get("voice_parameters"),
            ).model_dump(mode="json")
            available = voice_state["ready_for_inference"]

            def workflow_state(kind: NodeKind, stage: MotionStage) -> dict[str, Any]:
                node = next((n for n in project.nodes if n.kind == kind), None)
                recipe = node.recipe if node is not None else {}
                duration = (config or {}).get("preparation", {}).get("duration_s", 10)
                result = self.motion_readiness(
                    recipe.get("workflow"),
                    stage=stage,
                    parameters=recipe.get("parameters") or {},
                    profile=recipe.get("comfyui_profile"),
                    duration_s=duration,
                )
                limit = result.get("max_duration_s")
                if limit is not None and duration > limit:
                    result["ready"] = False
                    result["reasons"].append(
                        "Configured interval exceeds the workflow duration limit"
                    )
                return result

            motion_state = workflow_state(NodeKind.MOTION_VIDEO, "motion_video")
            lipsync_state = workflow_state(NodeKind.PERFORMANCE_LIPSYNC, "performance_lipsync")
            inputs_ready = not source_reasons and separation_state["ready"] and available
            generation_ready = bool(
                inputs_ready
                and motion_state["ready"]
                and (not (config or {}).get("lipsync") or lipsync_state["ready"])
            )
            duration = (config or {}).get("preparation", {}).get("duration_s", 10)
            return {
                "configured": bool(config),
                "source_preparation": {"ready": not source_reasons, "reasons": source_reasons},
                "separation": separation_state,
                "voice": voice_state,
                "motion": motion_state,
                "lipsync": lipsync_state,
                "ready_for_preview": bool(inputs_ready),
                "ready_for_video_preview": generation_ready and duration <= 10,
                "ready_for_generation": generation_ready,
                "quality_verified": False,
            }

    def prepare(self, project_id: str) -> dict[str, Any]:
        return self._schedule(project_id, "prepare")

    def segment_plan(
        self, project_id: str, request: SegmentPlanRequest | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Inspect a longer interval without changing the saved preview or scheduling it."""
        from avd.executors.handlers import _data_path

        request = SegmentPlanRequest.model_validate(request)
        root = self.runtime.settings.data_dir.expanduser().resolve()
        with self.runtime.session_factory() as session:
            project = self.project(session, project_id)
            if not project.performance_config:
                raise PerformanceUnavailable("Configure performance inputs before planning")
            prepare = next(
                (n for n in project.nodes if n.kind == NodeKind.PERFORMANCE_PREPARE), None
            )
            motion = next((n for n in project.nodes if n.kind == NodeKind.MOTION_VIDEO), None)
            if (
                prepare is None
                or prepare.status != NodeStatus.CLEAN
                or prepare.current_version is None
                or prepare.current_version.node_id != prepare.id
            ):
                raise PerformanceUnavailable(
                    "Complete the current source preparation before planning"
                )
            workflow = motion.recipe.get("workflow") if motion is not None else None
            if not isinstance(workflow, dict):
                raise PerformanceUnavailable("Configure a frozen motion workflow before planning")
            try:
                outputs = prepare.current_version.output_paths
                if not isinstance(outputs, dict):
                    raise ValueError("Source preparation has no manifest output mapping")
                manifest_path = _data_path(root, outputs.get("manifest"), existing=True)
                prepared = PreparedPerformance.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
                configuration = project.performance_config["preparation"]
                expected_hash = prepare.recipe.get("source_sha256")
                if (
                    prepared.request.model_dump(mode="json") != configuration
                    or prepared.source.path != configuration["source_video_path"]
                    or prepared.source.sha256 != expected_hash
                    or file_hash(root, configuration["source_video_path"]) != expected_hash
                ):
                    raise ValueError(
                        "Source or preparation changed; save and prepare the current inputs again"
                    )
                return plan_performance_interval(
                    prepared,
                    workflow,
                    duration_s=request.duration_s,
                    context_frames=request.context_frames,
                )
            except (OSError, ValueError, KeyError) as exc:
                raise PerformanceUnavailable(str(exc)) from exc

    def preview(self, project_id: str) -> dict[str, Any]:
        return self._schedule(project_id, "preview")

    def video_preview(self, project_id: str) -> dict[str, Any]:
        return self._schedule(project_id, "video_preview")

    def generate(self, project_id: str) -> dict[str, Any]:
        return self._schedule(project_id, "generate")

    def _schedule(
        self, project_id: str, stage: Literal["prepare", "preview", "video_preview", "generate"]
    ) -> dict[str, Any]:
        runtime = self.runtime
        kinds = (
            {NodeKind.PERFORMANCE_PREPARE}
            if stage == "prepare"
            else {
                NodeKind.PERFORMANCE_PREPARE,
                NodeKind.VOCAL_SEPARATION,
                NodeKind.SCENE_REFERENCE,
                NodeKind.SINGING_VOICE,
                NodeKind.PERFORMANCE_MIX,
            }
            if stage == "preview"
            else None
        )
        with runtime.session_factory() as session:
            project = self.project(session, project_id)
            if not project.performance_config:
                raise PerformanceUnavailable("Configure performance inputs before preparation")
            if (
                stage == "video_preview"
                and project.performance_config["preparation"]["duration_s"] > 10
            ):
                raise PerformanceUnavailable(
                    "A video preview requires an explicit interval within the 10-second limit; "
                    "save a shorter interval without changing the source timeline"
                )
            if stage != "prepare":
                require_review(
                    session,
                    project,
                    "performance_preview" if stage == "generate" else "performance_inputs",
                )
        if stage != "prepare":
            state = self.readiness(project_id)
            readiness_key = "ready_for_preview" if stage == "preview" else "ready_for_generation"
            if not state[readiness_key]:
                raise PerformanceUnavailable(
                    "Performance inputs, models or workflows are not ready", state
                )
        with runtime.session_factory() as session:
            project = self.project(session, project_id)
            plan = ProjectRepo(session).plan_rebuild(project)
            estimate = sum(
                runtime.engine.estimate_cost(n) for n in plan if kinds is None or n.kind in kinds
            )
            reserved = reserved_cost(session, project_id)
            confirmed = budget_is_confirmed(session, project, estimate + reserved)
            BudgetGuard(runtime.settings.confirm_threshold).check(
                project, estimate, confirmed=confirmed
            )
            if (
                project.budget_limit is not None
                and project.spent + reserved + estimate > project.budget_limit
            ):
                raise BudgetExceeded("Pending work plus this batch exceeds the project budget")
        jobs = runtime.engine.rebuild(project_id, kinds=kinds, confirmed=confirmed)
        return {
            "job_ids": jobs,
            "stage": {
                "prepare": "source_preparation",
                "preview": "reference_and_singing_preview",
                "video_preview": "performance_video_preview",
                "generate": "performance_generation",
            }[stage],
            "model_generated": False,
        }
