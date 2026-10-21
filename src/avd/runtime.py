"""Application composition shared by HTTP, director, and command-line entry points."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from avd.config import LLMRoute, Settings
from avd.engine import RebuildEngine
from avd.engine.rebuild import JOB_TYPES, _hash
from avd.jobs import HandlerRegistry, Job, JobQueue, JobStatus, Worker
from avd.jobs.execution import captured_route, record_request, route_snapshot
from avd.jobs.models import JobLane
from avd.jobs.native import native_execution_observer
from avd.jobs.process import ProcessRunner
from avd.llm import BudgetExceeded, CostLedger, LLMRouter
from avd.llm.budget import acquire_project_budget
from avd.llm.providers import Provider
from avd.llm.reservations import reserved_cost
from avd.observability import configure_logging, log_context
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, Project

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuntimeJobHandler:
    """Serializable production handler; database connections stay in their process."""

    settings: Settings

    def __call__(self, claimed: Job) -> dict[str, Any]:
        configure_logging(self.settings.log_format, self.settings.log_level)
        with log_context(project_id=claimed.project_id, job_id=claimed.id):
            runtime = Runtime(self.settings)
            try:
                job = runtime.queue.get(claimed.id)
                if (
                    job is None
                    or job.status != JobStatus.RUNNING
                    or job.claim_token != claimed.claim_token
                ):
                    raise ValueError("Job attempt no longer belongs to this worker")
                handler = runtime.build_registry().get(job.job_type)
                if handler is None:
                    raise ValueError(f"Unknown job_type: {job.job_type}")
                return handler(job)
            finally:
                runtime.close()


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        session_factory: Callable[[], Session] | None = None,
        providers: Mapping[str, Provider] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = None
        if session_factory is None:
            # Register application-layer tables without state importing its consumers.
            from avd.assets.media import MediaAsset  # noqa: F401
            from avd.director.mindmap import MindMapNode  # noqa: F401
            from avd.director.models import ChatMessage  # noqa: F401
            from avd.director.review import Review  # noqa: F401
            from avd.director.subagents import SubAgent  # noqa: F401
            from avd.performance.voice import init_voice_models

            self.database = create_engine(
                self.settings.database_url, data_dir=self.settings.data_dir
            )
            init_db(self.database)
            init_voice_models(self.database)
            session_factory = create_session_factory(self.database)
        self.session_factory = session_factory
        self.providers = dict(providers or {})
        self.queue = JobQueue(self.session_factory)
        self.engine = RebuildEngine(
            self.session_factory, self.queue, self.settings, before_schedule=self._check_schedule
        )

    def _check_schedule(
        self, session: Session, project: Project, node: Node, estimate: float
    ) -> None:
        from avd.director.review import budget_is_confirmed, generation_review_stage, require_review

        stage = generation_review_stage(project, node.kind)
        if stage:
            require_review(session, project, stage)
        self.engine.budget_guard.check(
            project,
            estimate,
            confirmed=budget_is_confirmed(
                session, project, estimate + self._reserved(session, project.id)
            ),
        )

    def router(self, session: Session, project: Project, *, job_id: str | None = None) -> LLMRouter:
        from avd.director.review import budget_is_confirmed

        job = session.get(Job, job_id) if job_id else None
        snapshot = captured_route(job) if job else None

        def observe(role: str, route: LLMRoute) -> None:
            if job is not None:
                record_request(job, role, route)

        def confirmation(current: Project, estimate: float) -> bool:
            reserved = self._reserved(session, current.id, exclude=job_id)
            if (
                current.budget_limit is not None
                and current.spent + reserved + estimate > current.budget_limit
            ):
                raise BudgetExceeded("Pending work plus this call exceeds the project budget")
            return budget_is_confirmed(session, current, estimate + reserved)

        return LLMRouter(
            self.settings,
            providers=self.providers,
            ledger=CostLedger(session, self.settings.model_prices),
            project=project,
            confirmation_check=confirmation,
            route_overrides={snapshot[0]: snapshot[1]} if snapshot else None,
            request_observer=observe if job else None,
        )

    @staticmethod
    def _reserved(session: Session, project_id: str, *, exclude: str | None = None) -> float:
        return reserved_cost(session, project_id, exclude=exclude)

    def build_registry(self) -> HandlerRegistry:
        from avd.agents.analysis import AnalysisPipeline, StructureTemplate
        from avd.agents.script import ScriptAgent
        from avd.director.review import require_review
        from avd.executors import build_registry
        from avd.state.models import Project
        from avd.state.repo import ProjectRepo

        def mark_paid(job: Job) -> None:
            with self.session_factory() as session:
                saved = session.get(Job, job.id)
                if saved is None:
                    raise LookupError("Job no longer exists")
                payload = dict(saved.payload)
                payload["_started_paid_attempts"] = sorted(
                    set(payload.get("_started_paid_attempts", [])) | {job.attempts}
                )
                saved.payload = payload
                session.commit()

        def image_requested(job: Job, provider: str, model: str) -> None:
            # Image handlers dispatch outside DB sessions. Commit this short write
            # before the network request so isolated-worker failures retain it.
            with self.session_factory() as session:
                saved = session.get(Job, job.id)
                if (
                    saved is None
                    or saved.status != JobStatus.RUNNING
                    or saved.claim_token != job.claim_token
                ):
                    raise ValueError("Image job attempt no longer belongs to this worker")
                record_request(saved, "image", LLMRoute(provider=provider, model=model))
                session.commit()

        def native_requested(job: Job, event: dict[str, Any]) -> None:
            # Serialize ownership validation and append in one short transaction.
            # Never trust a late spawned worker's detached Job snapshot.
            with self.session_factory() as session:
                if session.get_bind().dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                saved = session.get(Job, job.id)
                if (
                    saved is None
                    or saved.status != JobStatus.RUNNING
                    or not job.claim_token
                    or saved.claim_token != job.claim_token
                    or saved.attempts != job.attempts
                ):
                    raise ValueError("Native model job attempt no longer belongs to this worker")
                record_request(
                    saved,
                    str(event["role"]),
                    LLMRoute(provider=event["provider"], model=event["model"]),
                    phase=event["phase"],
                    details=event["details"],
                    source=event["source"],
                )
                session.commit()

        from avd.performance.backend import LocalPerformanceBackend

        registry = build_registry(
            self.settings,
            before_paid=mark_paid,
            image_request_observer=image_requested,
            performance_backend=LocalPerformanceBackend(self.settings, self.session_factory),
        )
        sheet_image_handler = registry.get("image_gen")
        assert sheet_image_handler is not None

        def guard(
            handler: Callable[[Job], dict[str, Any]], *, project_checks: bool = True
        ) -> Callable[[Job], dict[str, Any]]:
            def execute(job: Job) -> dict[str, Any]:
                if project_checks:
                    self.check_job(job)
                with native_execution_observer(lambda event: native_requested(job, event)):
                    return handler(job)

            return execute

        for _, job_type in JOB_TYPES.values():
            handler = registry.get(job_type)
            assert handler is not None
            registry.register(job_type, guard(handler))

        def authoring(job: Job) -> dict[str, Any]:
            with self.session_factory() as session:
                project = session.get(Project, job.project_id)
                if project is None:
                    raise KeyError("Authoring job project was not found")
                repo = ProjectRepo(session)
                router = self.router(session, project, job_id=job.id)
                try:
                    if job.job_type == "analyze_video":
                        template = AnalysisPipeline(
                            repo,
                            router,
                            self.settings,
                            execution_observer=router.request_observer,
                        ).analyze_video(project)
                        result = {
                            "summary": "Structure analysis ready for review",
                            "template": template.model_dump(mode="json"),
                        }
                    else:
                        require_review(session, project, "structure")
                        if project.character is None or project.voice is None:
                            raise ValueError("Attach consented assets before writing a script")
                        agent = ScriptAgent(repo, router, self.settings)
                        assert router.ledger is not None
                        before_entries = {entry.id for entry in router.ledger.list(project.id)}
                        board = agent.write_storyboard(
                            StructureTemplate.model_validate(project.analysis_result),
                            project.character,
                            project.voice,
                            job.payload["brief"],
                            project=project,
                        )
                        cost = sum(
                            entry.est_cost
                            for entry in router.ledger.list(project.id)
                            if entry.id not in before_entries and entry.role == "script"
                        )
                        agent.apply_storyboard(project, board, cost=cost)
                        result = {
                            "summary": "Script ready for review",
                            "storyboard": board.model_dump(mode="json"),
                        }
                finally:
                    # Persist usage from completed paid calls even when validation fails.
                    session.commit()
                return result

        registry.register("analyze_video", authoring)
        registry.register("write_script", authoring)

        def director_report(job: Job) -> dict[str, Any]:
            from avd.director.agent import DirectorAgent

            assert job.project_id is not None
            reply = DirectorAgent(self).chat(
                job.project_id, job.payload["text"], event=True, event_id=job.id
            )
            return {"summary": "Director reported", "reply": reply[:2000]}

        registry.register("director_report", director_report)

        def subagent_turn(job: Job) -> dict[str, Any]:
            from avd.director.subagents import run_worker_turn

            reply = run_worker_turn(self, job.payload["agent_id"], job_id=job.id)
            return {"summary": "Sub-agent reported", "reply": reply[:2000]}

        registry.register("subagent_turn", subagent_turn)

        def singing_model_probe(job: Job) -> dict[str, Any]:
            from avd.performance.voice import (
                RVCInstallation,
                SeedVCInstallation,
                voice_model_readiness,
            )

            captured = job.payload.get("rvc_installation")
            installation = RVCInstallation.model_validate(captured) if captured else None
            seed_captured = job.payload.get("seed_vc_installation")
            seed_installation = (
                SeedVCInstallation.model_validate(seed_captured) if seed_captured else None
            )
            with self.session_factory() as session:
                result = voice_model_readiness(
                    session,
                    self.settings.data_dir,
                    job.payload["version_id"],
                    installation=installation,
                    seed_installation=seed_installation,
                    probe=True,
                )
            return {"readiness": result.model_dump(mode="json")}

        registry.register("singing_model_probe", guard(singing_model_probe, project_checks=False))
        from avd.assets.sheets import make_sheet_handler

        registry.register("character_sheet", make_sheet_handler(self, guard(sheet_image_handler)))
        return registry

    def build_worker(self, lane: JobLane | str) -> Worker:
        """Build the production supervisor with per-job process isolation."""
        from avd.performance.remote import verify_remote_release

        if self.providers or self.database is None:
            raise ValueError(
                "Isolated workers require a persistent Runtime without injected providers; "
                "construct an inline Worker explicitly for fake backends"
            )
        registry = HandlerRegistry()
        handler = RuntimeJobHandler(self.settings)
        job_types = {value[1] for value in JOB_TYPES.values()}
        job_types.update(
            {
                "analyze_video",
                "write_script",
                "character_sheet",
                "director_report",
                "subagent_turn",
                "singing_model_probe",
            }
        )
        for job_type in job_types:
            registry.register(job_type, handler)
        return Worker(
            self.queue,
            lane,
            registry,
            self.on_finished,
            runner=ProcessRunner(),
            timeout_seconds=self.settings.job_timeout_seconds,
            job_timeouts=self.settings.job_timeouts,
            shutdown_grace_seconds=self.settings.worker_shutdown_grace_seconds,
            failure_guard=verify_remote_release,
        )

    def check_job(self, job: Job) -> None:
        """Recheck reviews, consent and budget immediately before executor work."""
        from avd.director.review import budget_is_confirmed, require_review

        if job.project_id:
            if job.node_id:
                self.engine._reconcile(job.project_id)
            elif job.job_type == "character_sheet":
                self.on_finished(job)

        with self.session_factory() as session:
            node = session.get(Node, job.node_id) if job.node_id else None
            project = node.project if node else session.get(Project, job.project_id)
            if project is None or (node is None and job.job_type != "character_sheet"):
                raise LookupError("Generation job has no project node")
            acquire_project_budget(project)
            snapshot = job.payload.get("recipe", {}).get("_execution")
            if (
                node is not None
                and job.payload.get("recipe_hash") is not None
                and _hash(self.engine._effective_recipe(node, execution=snapshot))
                != job.payload["recipe_hash"]
            ):
                raise ValueError("Job recipe changed while queued; schedule the current recipe")
            if node is not None:
                self.engine._consent(project, node)
                from avd.director.review import generation_review_stage

                stage = generation_review_stage(project, node.kind)
                if stage:
                    require_review(session, project, stage)
            estimate = float(job.payload.get("estimated_cost", 0))
            reserved = self._reserved(session, project.id, exclude=job.id)
            if (
                project.budget_limit is not None
                and project.spent + estimate + reserved > project.budget_limit
            ):
                raise BudgetExceeded("Pending work plus this execution exceeds the project budget")
            self.engine.budget_guard.check(
                project,
                estimate,
                confirmed=budget_is_confirmed(session, project, estimate + reserved),
            )

    def _report_analysis(self, job: Job) -> None:
        """Ask the director to report once analysis finally succeeds or fails."""
        if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED}:
            return  # A failed attempt that will be retried.
        from avd.director.agent import ANALYSIS_FAILED_EVENT, ANALYSIS_READY_EVENT

        if job.status == JobStatus.SUCCEEDED:
            text = ANALYSIS_READY_EVENT
        else:
            error = (job.error or "unknown error").strip().splitlines()
            text = ANALYSIS_FAILED_EVENT.format(error=(error[-1] if error else "")[:500])
        self.queue.submit(
            "api",
            "director_report",
            {
                "project_id": job.project_id,
                "text": text,
                "llm_route": route_snapshot(self.settings, "director"),
            },
            project_id=job.project_id,
            model_key=self.settings.llm_route("director").model,
            max_attempts=2,
        )

    def on_finished(self, job: Job) -> None:
        """Publish results, then let watchers and the director know (after publishing)."""
        self._on_finished(job)
        if job.job_type == "analyze_video":
            return  # analysis has its own report
        try:
            from avd.director.subagents import on_job_finished

            on_job_finished(self, job)
        except Exception:
            _LOGGER.exception("Could not notify watchers for job %s", job.id)

    def _on_finished(self, job: Job) -> None:
        """Publish generation nodes or account a global character-sheet attempt."""
        if job.node_id:
            self.engine.on_finished(job)
            return
        if job.job_type == "analyze_video" and job.project_id:
            self._report_analysis(job)
            return
        if job.job_type != "character_sheet" or not job.project_id:
            return
        with self.session_factory() as session:
            saved = session.get(Job, job.id)
            project = session.get(Project, job.project_id)
            if saved is None or project is None:
                return
            attempts = set(saved.payload.get("_started_paid_attempts", []))
            if saved.status == JobStatus.SUCCEEDED:
                attempts.add(saved.attempts)
            for attempt in attempts:
                if attempt <= saved.attempts:
                    cost = float(saved.payload.get("estimated_cost", 0))
                    if attempt == saved.attempts and saved.status == JobStatus.SUCCEEDED:
                        cost = float((saved.result or {}).get("cost", cost))
                    CostLedger(session).record_attempt(
                        project,
                        job_id=saved.id,
                        attempt=attempt,
                        role="character_sheet",
                        provider=str(
                            ((saved.payload.get("recipe") or {}).get("_execution") or {}).get(
                                "image_provider", self.settings.image_provider
                            )
                        ),
                        model=saved.model_key or self.settings.image_route()[1],
                        cost=cost,
                    )
            session.commit()

    def close(self) -> None:
        if self.database is not None:
            self.database.dispose()


__all__ = ["Runtime", "RuntimeJobHandler"]
