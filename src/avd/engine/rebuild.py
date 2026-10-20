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
        if node.kind in {NodeKind.SHOT_FRAME, NodeKind.LIPSYNC}:
            if project.character is None:
                raise ValueError("Project needs a consented character")
            require_usable(project.character)
        if node.kind in {NodeKind.VOICEOVER, NodeKind.LIPSYNC}:
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
        if node.kind == NodeKind.SHOT_FRAME:
            return {"image_model": self.settings.image_model}
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
        confirmed: bool = False,
    ) -> list[str]:
        project_id = project if isinstance(project, str) else project.id
        selected = {NodeKind(kind) for kind in kinds} if kinds is not None else set(JOB_TYPES)
        # Finished work must survive a budget, consent or template error during planning.
        self._reconcile(project_id)
        with self.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            current = session.get(Project, project_id)
            if current is None:
                raise KeyError(f"Project {project_id} was not found")
            repo = ProjectRepo(session)
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
            reserved = reserved_cost(session, project_id)
            job_ids: list[str] = []
            for node in plan:
                if (
                    node.kind not in selected
                    or node.kind not in JOB_TYPES
                    or node.id in active_nodes
                ):
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
                    "confirmed": confirmed,
                }
                model_key = (
                    node.recipe["_execution"]["image_model"]
                    if job_type == "image_gen"
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
        provider = (
            "openai"
            if job.job_type == "image_gen"
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
            confirmed = bool(saved.payload.get("confirmed", False))
            session.commit()
        self.rebuild(job.project_id, kinds=selected, confirmed=confirmed)


def rebuild(project: Project | str, *, engine: RebuildEngine, **kwargs: Any) -> list[str]:
    return engine.rebuild(project, **kwargs)


__all__ = ["JOB_TYPES", "RebuildEngine", "rebuild"]
