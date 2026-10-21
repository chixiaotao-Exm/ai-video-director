"""Schedule ready nodes, publish job results, and reuse dependency-aware versions."""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable, Iterable
from copy import deepcopy
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from avd.backends.workflows import DEFAULT_WORKFLOWS_DIR, WorkflowTemplateError
from avd.config import Settings
from avd.jobs import Job, JobQueue, JobStatus
from avd.llm import BudgetExceeded, BudgetGuard, CostLedger
from avd.llm.reservations import reserved_cost
from avd.performance.seed_vc import seed_installation_snapshot
from avd.performance.voice import rvc_installation_snapshot
from avd.state.consent import require_usable
from avd.state.models import Node, NodeKind, NodeStatus, Project, Version
from avd.state.repo import ProjectRepo

JOB_TYPES: dict[NodeKind, tuple[str, str]] = {
    NodeKind.SHOT_FRAME: ("api", "image_gen"),
    NodeKind.SHOT_VIDEO: ("gpu", "image_to_video"),
    NodeKind.VOICEOVER: ("gpu", "tts"),
    NodeKind.LIPSYNC: ("gpu", "lipsync"),
    NodeKind.SUBTITLES: ("cpu", "subtitles"),
    NodeKind.FINAL: ("cpu", "final"),
    NodeKind.PERFORMANCE_PREPARE: ("cpu", "performance_prepare"),
    NodeKind.VOCAL_SEPARATION: ("gpu", "vocal_separation"),
    NodeKind.SCENE_REFERENCE: ("api", "scene_reference"),
    NodeKind.SINGING_VOICE: ("gpu", "singing_voice"),
    NodeKind.MOTION_VIDEO: ("gpu", "motion_video"),
    NodeKind.PERFORMANCE_MIX: ("cpu", "performance_mix"),
    NodeKind.PERFORMANCE_SYNC: ("cpu", "performance_sync"),
    NodeKind.PERFORMANCE_LIPSYNC: ("gpu", "performance_lipsync"),
    NodeKind.PERFORMANCE_FINAL: ("cpu", "performance_final"),
}


def _hash(recipe: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            recipe, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


class RebuildEngine:
    """Own short transactions; callers commit authoring changes before rebuild.

    ``rebuild`` only queues ready work. Pass ``on_finished`` to workers, or call
    rebuild again after a restart to reconcile already-finished queue records.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        queue: JobQueue,
        settings: Settings | None = None,
        *,
        budget_guard: BudgetGuard | None = None,
        estimate_cost: Callable[[Node], float] | None = None,
        before_schedule: Callable[[Session, Project, Node, float], None] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.queue = queue
        self.settings = settings or Settings()
        self.budget_guard = budget_guard or BudgetGuard(self.settings.confirm_threshold)
        self.estimate_cost = estimate_cost or self._estimate
        self.before_schedule = before_schedule

    def _estimate(self, node: Node) -> float:
        job_type = JOB_TYPES[node.kind][1]
        return self.settings.job_estimated_costs.get(job_type, 0.0)

    @staticmethod
    def _consent(project: Project, node: Node) -> None:
        if node.kind in {
            NodeKind.SHOT_FRAME,
            NodeKind.LIPSYNC,
            NodeKind.SCENE_REFERENCE,
            NodeKind.MOTION_VIDEO,
            NodeKind.PERFORMANCE_LIPSYNC,
        }:
            if project.character is None:
                raise ValueError("Project needs a consented character")
            require_usable(project.character)
        if node.kind in {
            NodeKind.VOICEOVER,
            NodeKind.LIPSYNC,
            NodeKind.SINGING_VOICE,
            NodeKind.PERFORMANCE_LIPSYNC,
        }:
            if project.voice is None:
                raise ValueError("Project needs a consented voice")
            require_usable(project.voice)

    @staticmethod
    def _fingerprint(node: Node) -> dict[str, Any]:
        # Authoring nodes are expanded into per-shot recipes by ScriptAgent.
        # Using the whole script hash here would invalidate unaffected shots.
        return {
            dependency.id: {
                "recipe_hash": dependency.current_version.recipe_hash,
                "outputs": deepcopy(dependency.current_version.output_paths),
            }
            for dependency in sorted(node.depends_on, key=lambda item: item.id)
            if dependency.kind in JOB_TYPES and dependency.current_version is not None
        }

    def _execution_settings(self, node: Node) -> dict[str, Any]:
        if node.kind in {NodeKind.SHOT_FRAME, NodeKind.SCENE_REFERENCE}:
            provider, model = self.settings.image_route()
            return {"image_provider": provider, "image_model": model}
        if node.kind in {NodeKind.SHOT_VIDEO, NodeKind.VOICEOVER, NodeKind.LIPSYNC}:
            name = JOB_TYPES[node.kind][1]
            directory = self.settings.workflow_dir or DEFAULT_WORKFLOWS_DIR
            template = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
            if not isinstance(template, dict):
                raise WorkflowTemplateError("workflow template must be a JSON object")
            return {
                "workflow_name": name,
                "workflow_template": template,
                "comfyui_profile": self.settings.comfyui_profile.model_dump(mode="json"),
            }
        if node.kind == NodeKind.SINGING_VOICE:
            if (node.recipe or {}).get("voice_engine") == "seed-vc":
                return {
                    "seed_vc_runtime": seed_installation_snapshot(
                        self.settings.seed_vc_installation
                    )
                }
            return {"rvc_runtime": rvc_installation_snapshot(self.settings.rvc_installation)}
        if node.kind == NodeKind.PERFORMANCE_FINAL:
            return {"ai_label": True}
        return {}

    def _effective_recipe(
        self,
        node: Node,
        *,
        execution: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        recipe = deepcopy(node.recipe)
        recipe["_inputs"] = self._fingerprint(node)
        recipe["_execution"] = (
            self._execution_settings(node) if execution is None else deepcopy(execution)
        )
        return recipe

    @staticmethod
    def _inputs(node: Node) -> dict[str, Any]:
        inputs: dict[str, Any] = {}
        pending = list(node.depends_on)
        while pending:
            dependency = pending.pop()
            if dependency.id in inputs:
                continue
            version = dependency.current_version
            inputs[dependency.id] = {
                "version_id": version.id if version else None,
                "kind": dependency.kind.value,
                "shot_index": dependency.shot_index,
                "outputs": deepcopy(version.output_paths) if version else {},
                "recipe": deepcopy(dependency.recipe),
                "duration_s": dependency.recipe.get("duration_s"),
            }
            pending.extend(dependency.depends_on)
        return inputs

    @staticmethod
    def _context(project: Project) -> dict[str, Any]:
        script = next((n for n in project.nodes if n.kind == NodeKind.SCRIPT), None)
        character = project.character
        voice = project.voice
        return {
            "storyboard": deepcopy(script.recipe) if script else {},
            "character": {
                "id": character.id,
                "reference_images": deepcopy(character.reference_images),
                "fixed_appearance_prompt": character.fixed_appearance_prompt,
            }
            if character
            else {},
            "voice": {
                "id": voice.id,
                "sample_audio_paths": deepcopy(voice.sample_audio_paths),
                "engine": voice.engine,
                "engine_voice_id": voice.engine_voice_id,
                "default_params": deepcopy(voice.default_params),
                "model_path": voice.model_path,
            }
            if voice
            else {},
        }

    def rebuild(
        self,
        project: Project | str,
        *,
        kinds: Iterable[NodeKind | str] | None = None,
        node_ids: Iterable[str] | None = None,
        confirmed: bool = False,
        _automatic: bool = False,
    ) -> list[str]:
        project_id = project if isinstance(project, str) else project.id
        selected = {NodeKind(kind) for kind in kinds} if kinds is not None else set(JOB_TYPES)
        selected_ids = set(node_ids) if node_ids is not None else None
        # Finished work must survive a budget, consent or template error during planning.
        self._reconcile(project_id)
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            current = session.get(Project, project_id)
            if current is None:
                raise KeyError(f"Project {project_id} was not found")
            repo = ProjectRepo(session)
            for node in current.nodes:
                execution = node.recipe.get("_execution")
                changed_clean_runtime = (
                    node.kind in {NodeKind.SINGING_VOICE, NodeKind.PERFORMANCE_FINAL}
                    and node.status == NodeStatus.CLEAN
                )
                legacy_failed_final = (
                    not _automatic
                    and node.kind == NodeKind.PERFORMANCE_FINAL
                    and node.status == NodeStatus.FAILED
                    and (not isinstance(execution, dict) or execution.get("ai_label") is not True)
                )
                if changed_clean_runtime or legacy_failed_final:
                    effective = self._execution_settings(node)
                    if execution != effective:
                        repo.update_recipe(node, {"_execution": effective})
            plan = repo.plan_rebuild(current)
            active = list(
                session.scalars(
                    select(Job).where(
                        Job.project_id == project_id,
                        Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                    )
                )
            )
            active_nodes = {job.node_id for job in active}
            cancelled_nodes: set[str] = set()
            if _automatic:
                # A user's cancellation survives sibling completions and restarts.
                # An explicit rebuild or queue retry creates/reactivates a newer job,
                # so the cancellation stops suppressing that node naturally.
                latest: dict[str, JobStatus] = {}
                for job in session.scalars(
                    select(Job)
                    .where(Job.project_id == project_id, Job.node_id.is_not(None))
                    .order_by(Job.created_at.desc(), Job.id.desc())
                ):
                    assert job.node_id is not None
                    latest.setdefault(job.node_id, job.status)
                cancelled_nodes = {
                    node_id for node_id, status in latest.items() if status == JobStatus.CANCELLED
                }
            reserved = reserved_cost(session, project_id)
            job_ids: list[str] = []
            for node in plan:
                if (
                    node.kind not in selected
                    or node.kind not in JOB_TYPES
                    or node.id in active_nodes
                    or node.id in cancelled_nodes
                    or (selected_ids is not None and node.id not in selected_ids)
                ):
                    continue
                if node.kind == NodeKind.PERFORMANCE_LIPSYNC and not (
                    current.performance_config or {}
                ).get("lipsync"):
                    continue
                if any(parent.status != NodeStatus.CLEAN for parent in node.depends_on):
                    continue
                self._consent(current, node)
                node.recipe = self._effective_recipe(node)
                cached = repo.find_cached_version(node)
                if cached is not None and self._outputs_exist(cached.output_paths):
                    node.current_version = cached
                    node.current_version_id = cached.id
                    node.status = NodeStatus.CLEAN
                    continue
                cost = self.estimate_cost(node)
                if self.before_schedule is not None:
                    self.before_schedule(session, current, node, cost)
                self.budget_guard.check(current, cost, confirmed=confirmed)
                if (
                    current.budget_limit is not None
                    and current.spent + reserved + cost > current.budget_limit
                ):
                    raise BudgetExceeded("Pending work plus this job exceeds the project budget")
                lane, job_type = JOB_TYPES[node.kind]
                version_number = len(repo.list_versions(node)) + 1
                output_dir = f"projects/{current.id}/{node.id}/{version_number}-{uuid.uuid4().hex}"
                payload = {
                    "data_dir": str(self.settings.data_dir.expanduser().resolve()),
                    "output_dir": output_dir,
                    "recipe": deepcopy(node.recipe),
                    "recipe_hash": _hash(node.recipe),
                    "inputs": self._inputs(node),
                    "context": self._context(current),
                    "estimated_cost": cost,
                    "selected_kinds": sorted(kind.value for kind in selected),
                    "selected_node_ids": sorted(selected_ids) if selected_ids is not None else None,
                    "confirmed": confirmed,
                }
                model_key = (
                    node.recipe["_execution"]["image_model"]
                    if job_type in {"image_gen", "scene_reference"}
                    else job_type
                )
                job_ids.append(
                    self.queue.submit(
                        lane,
                        job_type,
                        payload,
                        model_key=model_key,
                        project_id=current.id,
                        node_id=node.id,
                        session=session,
                    )
                )
                node.status = NodeStatus.RUNNING
                reserved += cost
                active_nodes.add(node.id)
            session.commit()
            return job_ids

    def _reconcile(self, project_id: str) -> None:
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            if session.get(Project, project_id) is None:
                raise KeyError(f"Project {project_id} was not found")
            for job in session.scalars(select(Job).where(Job.project_id == project_id)).all():
                self._publish(session, job)
            session.commit()

    def retry_failed(
        self,
        project: Project | str,
        *,
        kinds: Iterable[NodeKind | str] | None = None,
        confirmed: bool = False,
    ) -> list[str]:
        """Explicitly resubmit failed nodes using their current recipes and settings."""
        project_id = project if isinstance(project, str) else project.id
        selected = {NodeKind(kind) for kind in kinds} if kinds is not None else set(JOB_TYPES)
        self._reconcile(project_id)
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            for node in session.scalars(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.status == NodeStatus.FAILED,
                    Node.kind.in_(selected),
                )
            ):
                node.status = NodeStatus.DIRTY
            session.commit()
        return self.rebuild(project_id, kinds=selected, confirmed=confirmed)

    @staticmethod
    def _output_path(root: Path, value: Any) -> Path:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("Output paths must be nonempty relative strings")
        relative = PurePosixPath(value.replace("\\", "/"))
        if relative.is_absolute() or PureWindowsPath(value).drive or ".." in relative.parts:
            raise ValueError("Output path must stay inside data_dir")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("Output file must exist and be nonempty inside data_dir")
        return path

    def _outputs_exist(self, outputs: Any) -> bool:
        if not isinstance(outputs, dict) or not outputs:
            return False
        root = self.settings.data_dir.expanduser().resolve()
        for value in outputs.values():
            try:
                self._output_path(root, value)
            except (OSError, TypeError, ValueError):
                return False
        return True

    def _validate_outputs(self, job: Job, outputs: Any) -> dict[str, str]:
        if not isinstance(outputs, dict) or not outputs:
            raise ValueError("Job result must contain nonempty output paths")
        root = self.settings.data_dir.expanduser().resolve()
        directory = (root / job.payload["output_dir"]).resolve()
        if not directory.is_relative_to(root):
            raise ValueError("Job output directory is outside data_dir")
        checked: dict[str, str] = {}
        for name, value in outputs.items():
            resolved = self._output_path(root, value)
            if not resolved.is_relative_to(directory):
                raise ValueError("Job output is missing or outside its version directory")
            checked[str(name)] = str(value)
        return checked

    @staticmethod
    def _record_attempt(
        session: Session, job: Job, project: Project, attempt: int, cost: float
    ) -> None:
        execution = (job.payload.get("recipe") or {}).get("_execution") or {}
        provider = (
            str(execution.get("image_provider", "openai"))
            if job.job_type in {"image_gen", "scene_reference"}
            else ("comfyui" if job.lane == "gpu" else "ffmpeg")
        )
        CostLedger(session).record_attempt(
            project,
            job_id=job.id,
            attempt=attempt,
            role=job.job_type,
            provider=provider,
            model=job.model_key or job.job_type,
            cost=cost,
        )

    def _record_failed_attempts(self, session: Session, job: Job, project: Project) -> None:
        started = job.payload.get("_started_paid_attempts", [])
        if not isinstance(started, list):
            return
        for attempt in sorted({value for value in started if type(value) is int and value > 0}):
            if attempt > job.attempts or (
                attempt == job.attempts and job.status in {JobStatus.RUNNING, JobStatus.SUCCEEDED}
            ):
                continue
            self._record_attempt(
                session,
                job,
                project,
                attempt,
                float(job.payload.get("estimated_cost", 0)),
            )

    def _publish(self, session: Session, job: Job) -> None:
        if not job.node_id or "recipe_hash" not in job.payload:
            return
        node = session.get(Node, job.node_id)
        if node is None:
            return
        self._record_failed_attempts(session, job, node.project)
        result = deepcopy(job.result or {})
        if result.get("_engine_handled"):
            return
        if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            return
        execution = job.payload["recipe"].get("_execution", {})
        matches = (
            _hash(self._effective_recipe(node, execution=execution)) == job.payload["recipe_hash"]
        )
        if job.status == JobStatus.CANCELLED:
            node.status = NodeStatus.DIRTY
        elif job.status == JobStatus.FAILED:
            node.status = NodeStatus.FAILED if matches else NodeStatus.DIRTY
        else:
            try:
                outputs = self._validate_outputs(job, result.get("outputs"))
                cost = float(result.get("cost", job.payload.get("estimated_cost", 0)))
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError("Cost must be finite and non-negative")
            except (OSError, TypeError, ValueError) as exc:
                job.status = JobStatus.FAILED
                job.error = f"Invalid executor result: {exc}"
                node.status = NodeStatus.FAILED if matches else NodeStatus.DIRTY
                self._record_failed_attempts(session, job, node.project)
            else:
                self._record_attempt(session, job, node.project, job.attempts or 1, cost)
                repo = ProjectRepo(session)
                if matches:
                    version = repo.record_version(node, outputs, cost, note=f"Job {job.id}")
                else:
                    # Preserve historical work without falsely clearing a concurrent edit.
                    versions = repo.list_versions(node)
                    version = Version(
                        node_id=node.id,
                        number=(versions[-1].number + 1 if versions else 1),
                        recipe_snapshot=deepcopy(job.payload["recipe"]),
                        output_paths=outputs,
                        recipe_hash=job.payload["recipe_hash"],
                        cost=cost,
                        note=f"Stale completion from job {job.id}",
                    )
                    session.add(version)
                    session.flush()
                    node.status = NodeStatus.DIRTY
                result["version_id"] = version.id
        result["_engine_handled"] = True
        job.result = result
        session.flush()

    def on_finished(self, job: Job) -> None:
        if not job.project_id or not job.node_id:
            return
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            saved = session.get(Job, job.id)
            if saved is None:
                return
            self._publish(session, saved)
            selected = saved.payload.get("selected_kinds")
            selected_ids = saved.payload.get("selected_node_ids")
            confirmed = bool(saved.payload.get("confirmed", False))
            session.commit()
        self.rebuild(
            job.project_id,
            kinds=selected,
            node_ids=selected_ids,
            confirmed=confirmed,
            _automatic=True,
        )


def rebuild(project: Project | str, *, engine: RebuildEngine, **kwargs: Any) -> list[str]:
    return engine.rebuild(project, **kwargs)


__all__ = ["JOB_TYPES", "RebuildEngine", "rebuild"]
