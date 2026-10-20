"""Budget decisions remain exclusive until costs or reservations are committed."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from avd.assets.sheets import submit_character_sheet
from avd.config import ModelPrice, Settings
from avd.director.tools import DirectorTools
from avd.llm import (
    BudgetBusy,
    BudgetExceeded,
    BudgetGuard,
    ChatResponse,
    CostEntry,
    CostLedger,
    FakeProvider,
)
from avd.llm.budget import acquire_project_budget
from avd.llm.reservations import reserved_cost
from avd.runtime import Runtime
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    current = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            model_prices={"gpt-6.1-sol": ModelPrice(output_per_million=100000)},
            job_estimated_costs={"image_gen": 0.6, "analyze_video": 0.6},
        ),
        providers={"openai": FakeProvider([ChatResponse(content="ok", output_tokens=6)])},
    )
    yield current
    current.close()


def project_id(runtime: Runtime) -> str:
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project(
            "Budget concurrency", source_video_path="uploads/source.mp4", budget_limit=1
        )
        session.commit()
        return project.id


def test_ledger_must_commit_before_second_session_can_spend(runtime: Runtime) -> None:
    identifier = project_id(runtime)
    with runtime.session_factory() as first, runtime.session_factory() as second:
        original = first.get(Project, identifier)
        stale = second.get(Project, identifier)
        assert original is not None and stale is not None and stale.spent == 0
        runtime.router(first, original).chat("script", [], estimated_cost=0.6)
        with pytest.raises(BudgetBusy, match="try again"):
            runtime.router(second, stale).chat("script", [], estimated_cost=0.6)
        assert len(runtime.providers["openai"].calls) == 1
        first.commit()
        with pytest.raises(BudgetExceeded, match="budget"):
            runtime.router(second, stale).chat("script", [], estimated_cost=0.6)
        assert stale.spent == pytest.approx(0.6)
        assert len(runtime.providers["openai"].calls) == 1
    with runtime.session_factory() as session:
        assert session.get(Project, identifier).spent == pytest.approx(0.6)
        assert len(list(session.scalars(select(CostEntry)))) == 1


def test_same_session_repairs_reuse_lock_and_nested_commit_does_not_release(
    runtime: Runtime,
) -> None:
    identifier = project_id(runtime)
    with runtime.session_factory() as first, runtime.session_factory() as second:
        project = first.get(Project, identifier)
        other = second.get(Project, identifier)
        BudgetGuard().check(project, 0.2)
        with first.begin_nested():
            BudgetGuard().check(project, 0.2)
        with pytest.raises(BudgetBusy):
            BudgetGuard().check(other, 0.2)
        first.rollback()
        BudgetGuard().check(other, 0.2)


def test_lock_is_per_project_and_preserves_intentional_budget_changes(runtime: Runtime) -> None:
    first_id, second_id = project_id(runtime), project_id(runtime)
    with runtime.session_factory() as first, runtime.session_factory() as second:
        one = first.get(Project, first_id)
        two = second.get(Project, second_id)
        assert one is not None and two is not None
        one.budget_limit = 2
        BudgetGuard(confirm_threshold=2).check(one, 1.5)
        BudgetGuard().check(two, 0.6)
        assert one.budget_limit == 2


def test_provider_call_holds_no_sqlite_write_lock_but_blocks_paid_scheduling(
    runtime: Runtime,
) -> None:
    identifier = project_id(runtime)

    class CheckingProvider(FakeProvider):
        def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
            # This scheduler starts BEGIN IMMEDIATE before checking the budget.
            # It must fail promptly on the project lock, not deadlock SQLite.
            with pytest.raises(BudgetBusy):
                submit_character_sheet(runtime, identifier, "Hero", "A fictional character")
            with pytest.raises(BudgetBusy):
                DirectorTools(runtime, identifier).execute("analyze_video", {})
            # A queue writer/heartbeat can still commit during the remote call.
            runtime.queue.submit("cpu", "unrelated", {}, project_id=identifier)
            return super().chat(*args, **kwargs)

    runtime.providers["openai"] = CheckingProvider([ChatResponse(content="ok", output_tokens=6)])
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        runtime.router(session, project).chat("script", [], estimated_cost=0.6)
        session.commit()
    assert [job.job_type for job in runtime.queue.list(identifier)] == ["unrelated"]
    with pytest.raises(BudgetExceeded):
        submit_character_sheet(runtime, identifier, "Hero", "A fictional character")


def test_queued_reservation_is_visible_after_scheduling_lock_releases(runtime: Runtime) -> None:
    identifier = project_id(runtime)
    with runtime.session_factory() as scheduler, runtime.session_factory() as caller:
        scheduled_project = scheduler.get(Project, identifier)
        stale = caller.get(Project, identifier)
        BudgetGuard().check(scheduled_project, 0.6)
        runtime.queue.submit(
            "api",
            "analyze_video",
            {"estimated_cost": 0.6},
            project_id=identifier,
            session=scheduler,
        )
        with pytest.raises(BudgetBusy):
            runtime.router(caller, stale).chat("script", [], estimated_cost=0.6)
        scheduler.commit()
        with pytest.raises(BudgetExceeded, match="Pending work"):
            runtime.router(caller, stale).chat("script", [], estimated_cost=0.6)
    assert not runtime.providers["openai"].calls


def test_independent_process_uses_same_database_lock(runtime: Runtime) -> None:
    identifier = project_id(runtime)
    script = """
import sys
from avd.llm import BudgetBusy, BudgetGuard
from avd.state.db import create_engine, create_session_factory
from avd.state.models import Project
engine = create_engine(sys.argv[1])
with create_session_factory(engine)() as session:
    try:
        BudgetGuard().check(session.get(Project, sys.argv[2]), 0.1)
    except BudgetBusy:
        print('busy')
    else:
        print('available')
engine.dispose()
"""

    def check() -> str:
        result = subprocess.run(
            [sys.executable, "-c", script, runtime.settings.database_url, identifier],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    with runtime.session_factory() as session:
        acquire_project_budget(session.get(Project, identifier))
        assert check() == "busy"
        session.rollback()
        assert check() == "available"


def test_failed_provider_caller_rollback_releases_lock(runtime: Runtime) -> None:
    identifier = project_id(runtime)

    class FailingProvider(FakeProvider):
        def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
            raise RuntimeError("offline provider failure")

    runtime.providers["openai"] = FailingProvider([])
    with runtime.session_factory() as session:
        with pytest.raises(RuntimeError, match="offline provider"):
            runtime.router(session, session.get(Project, identifier)).chat(
                "script", [], estimated_cost=0.6
            )
        session.rollback()
        with runtime.session_factory() as other:
            BudgetGuard().check(other.get(Project, identifier), 0.6)


def test_completed_cost_stays_reserved_until_active_budget_transaction_finishes(
    runtime: Runtime,
) -> None:
    identifier = project_id(runtime)
    job_id = runtime.queue.submit(
        "api", "character_sheet", {"estimated_cost": 0.6}, project_id=identifier
    )
    job = runtime.queue.claim_next("api")
    assert job is not None and job.id == job_id
    finished = runtime.queue.complete(job_id, {"cost": 0.6}, claim_token=job.claim_token)
    with runtime.session_factory() as session:
        from avd.jobs import Job

        saved = session.get(Job, job_id)
        saved.payload = {**saved.payload, "_started_paid_attempts": [1]}
        session.commit()
    with runtime.session_factory() as decision:
        project = decision.get(Project, identifier)
        acquire_project_budget(project)
        assert project.spent == 0
        assert reserved_cost(decision, identifier) == pytest.approx(0.6)
        with pytest.raises(BudgetBusy):
            runtime.on_finished(finished)
        assert not list(decision.scalars(select(CostEntry)))
        assert reserved_cost(decision, identifier) == pytest.approx(0.6)
        decision.commit()
    # A callback retry or restart reconciliation can publish the cost later.
    runtime.on_finished(finished)
    runtime.on_finished(finished)
    with runtime.session_factory() as session:
        assert session.get(Project, identifier).spent == pytest.approx(0.6)
        assert reserved_cost(session, identifier) == 0
        assert len(list(session.scalars(select(CostEntry)))) == 1


def test_rejected_direct_accounting_does_not_stage_an_unpaired_ledger_entry(
    runtime: Runtime,
) -> None:
    identifier = project_id(runtime)
    with runtime.session_factory() as decision, runtime.session_factory() as accounting:
        acquire_project_budget(decision.get(Project, identifier))
        project = accounting.get(Project, identifier)
        ledger = CostLedger(accounting, runtime.settings.model_prices)
        with pytest.raises(BudgetBusy):
            ledger.record(project, "script", "openai", "gpt-6.1-sol", 0, 6)
        accounting.commit()
        assert not list(decision.scalars(select(CostEntry)))
        decision.rollback()
        ledger.record(project, "script", "openai", "gpt-6.1-sol", 0, 6)
        accounting.commit()
        assert project.spent == pytest.approx(0.6)


def test_api_reports_busy_project_as_conflict(runtime: Runtime) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("multipart")
    from fastapi.testclient import TestClient

    from avd.api.app import create_app

    identifier = project_id(runtime)
    with runtime.session_factory() as session:
        acquire_project_budget(session.get(Project, identifier))
        with TestClient(create_app(runtime=runtime), base_url="http://127.0.0.1") as client:
            response = client.post(f"/projects/{identifier}/tools/analyze_video", json={"args": {}})
        assert response.status_code == 409
        assert "try again" in response.json()["detail"]
