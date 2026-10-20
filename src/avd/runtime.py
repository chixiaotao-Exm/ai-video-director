"""Application composition shared by HTTP, director, and command-line entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from avd.config import Settings
from avd.engine import RebuildEngine
from avd.engine.rebuild import JOB_TYPES, _hash
from avd.jobs import HandlerRegistry, Job, JobQueue
from avd.llm import CostLedger, LLMRouter
from avd.llm.providers import Provider
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, Project


class Runtime:
    def __init__(
        self, settings: Settings | None = None,
        session_factory: Callable[[], Session] | None = None,
        providers: Mapping[str, Provider] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.database = None
        if session_factory is None:
            self.database = create_engine(self.settings.database_url, data_dir=self.settings.data_dir)
            init_db(self.database)
            session_factory = create_session_factory(self.database)
        self.session_factory = session_factory
        self.providers = dict(providers or {})
        self.queue = JobQueue(self.session_factory)
        self.engine = RebuildEngine(self.session_factory, self.queue, self.settings)

    def router(self, session: Session, project: Any, *, confirmed: bool = False) -> LLMRouter:
        return LLMRouter(
            self.settings, providers=self.providers,
            ledger=CostLedger(session, self.settings.model_prices), project=project,
            confirmed=confirmed,
        )

    def build_registry(self) -> HandlerRegistry:
        from avd.agents.analysis import AnalysisPipeline, StructureTemplate
        from avd.agents.script import ScriptAgent
        from avd.director.review import require_review
        from avd.executors import build_registry
        from avd.state.models import Project
        from avd.state.repo import ProjectRepo

        registry = build_registry(self.settings)

        def guard(handler: Callable[[Job], dict[str, Any]]) -> Callable[[Job], dict[str, Any]]:
            def execute(job: Job) -> dict[str, Any]:
                self.check_job(job)
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
                router = self.router(session, project, confirmed=bool(job.payload.get("confirmed", False)))
                try:
                    if job.job_type == "analyze_video":
                        template = AnalysisPipeline(repo, router, self.settings).analyze_video(project)
                        result = {"summary": "Structure analysis ready for review", "template": template.model_dump(mode="json")}
                    else:
                        require_review(session, project, "structure")
                        if project.character is None or project.voice is None:
                            raise ValueError("Attach consented assets before writing a script")
                        agent = ScriptAgent(repo, router, self.settings)
                        board = agent.write_storyboard(
                            StructureTemplate.model_validate(project.analysis_result),
                            project.character, project.voice, job.payload["brief"], project=project,
                        )
                        agent.apply_storyboard(project, board)
                        result = {"summary": "Script ready for review", "storyboard": board.model_dump(mode="json")}
                finally:
                    # Persist usage from completed paid calls even when validation fails.
                    session.commit()
                return result

        registry.register("analyze_video", authoring)
        registry.register("write_script", authoring)
        return registry

    def check_job(self, job: Job) -> None:
        """Recheck reviews, consent and budget immediately before executor work."""
        from avd.director.review import budget_is_confirmed, require_review

        with self.session_factory() as session:
            node = session.get(Node, job.node_id)
            if node is None:
                raise LookupError("Generation job has no project node")
            project = node.project
            if job.payload.get("recipe_hash") is not None and _hash(self.engine._effective_recipe(node)) != job.payload["recipe_hash"]:
                raise ValueError("Job recipe changed while queued; schedule the current recipe")
            self.engine._consent(project, node)
            require_review(session, project, "script" if node.kind == NodeKind.SHOT_FRAME else "frames")
            estimate = float(job.payload.get("estimated_cost", 0))
            self.engine.budget_guard.check(
                project, estimate,
                confirmed=budget_is_confirmed(session, project, estimate),
            )

    def close(self) -> None:
        if self.database is not None:
            self.database.dispose()


__all__ = ["Runtime"]
