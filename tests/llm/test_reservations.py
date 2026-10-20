from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from avd.jobs.models import Job, JobLane, JobStatus
from avd.llm.ledger import CostLedger
from avd.llm.reservations import reserved_cost
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def make_job(
    session: Session,
    project: Project,
    *,
    status: JobStatus = JobStatus.QUEUED,
    attempts: int = 0,
    estimate: float = 0.5,
    markers: Any = None,
    job_type: str = "image_gen",
    result: dict[str, Any] | None = None,
    node: bool = True,
) -> Job:
    graph_node = Node(project_id=project.id, kind=NodeKind.SHOT_FRAME, recipe={}) if node else None
    if graph_node is not None:
        session.add(graph_node)
        session.flush()
    payload: dict[str, Any] = {"estimated_cost": estimate}
    if markers is not None:
        payload["_started_paid_attempts"] = markers
    job = Job(
        project_id=project.id,
        node_id=graph_node.id if graph_node else None,
        lane=JobLane.API,
        job_type=job_type,
        payload=payload,
        status=status,
        attempts=attempts,
        result=result,
    )
    session.add(job)
    session.flush()
    return job


def charge(session: Session, project: Project, job: Job, attempt: int, cost: float) -> None:
    CostLedger(session).record_attempt(
        project,
        job_id=job.id,
        attempt=attempt,
        role=job.job_type,
        provider="fake",
        model="fake",
        cost=cost,
    )


def test_active_estimates_are_project_scoped_and_current_paid_work_is_not_counted_twice(
    session: Session,
) -> None:
    project = ProjectRepo(session).create_project("reservations")
    other = ProjectRepo(session).create_project("other")
    make_job(session, project, estimate=0.2)
    running = make_job(
        session, project, status=JobStatus.RUNNING, attempts=1, estimate=0.3, markers=[1]
    )
    make_job(session, other, estimate=90)
    make_job(session, project, status=JobStatus.FAILED, attempts=1, estimate=90)
    assert reserved_cost(session, project.id) == pytest.approx(0.5)
    assert reserved_cost(session, project.id, exclude=running.id) == pytest.approx(0.2)


@pytest.mark.parametrize("status", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_paid_attempt_stays_reserved_until_ledger_is_written(
    session: Session, status: JobStatus
) -> None:
    project = ProjectRepo(session).create_project("terminal")
    job = make_job(session, project, status=status, attempts=1, markers=[1])
    assert project.spent == 0
    assert reserved_cost(session, project.id, exclude=job.id) == 0.5
    charge(session, project, job, 1, 0.5)
    assert project.spent == 0.5
    assert reserved_cost(session, project.id) == 0


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_retry_keeps_unaccounted_history_when_current_estimate_is_excluded(
    session: Session, status: JobStatus
) -> None:
    project = ProjectRepo(session).create_project("retry")
    job = make_job(session, project, status=status, attempts=2, markers=[1, 2])
    prior_count = 2 if status == JobStatus.QUEUED else 1
    assert reserved_cost(session, project.id) == 0.5 * (prior_count + 1)
    assert reserved_cost(session, project.id, exclude=job.id) == 0.5 * prior_count
    charge(session, project, job, 1, 0.5)
    assert reserved_cost(session, project.id) == 0.5 * prior_count
    assert reserved_cost(session, project.id, exclude=job.id) == 0.5 * (prior_count - 1)


@pytest.mark.parametrize("job_type,node", [("image_gen", True), ("character_sheet", False)])
@pytest.mark.parametrize("markers", [None, [1, 2]])
def test_success_reserves_result_cost_even_without_paid_marker_and_releases_after_accounting(
    session: Session, job_type: str, node: bool, markers: list[int] | None
) -> None:
    project = ProjectRepo(session).create_project("success")
    job = make_job(
        session,
        project,
        status=JobStatus.SUCCEEDED,
        attempts=2,
        markers=markers,
        job_type=job_type,
        result={"cost": 0.3},
        node=node,
    )
    historical = 0.5 if markers else 0
    assert reserved_cost(session, project.id, exclude=job.id) == pytest.approx(historical + 0.3)
    charge(session, project, job, 2, 0.3)
    assert reserved_cost(session, project.id) == historical
    if markers:
        charge(session, project, job, 1, 0.5)
    assert reserved_cost(session, project.id) == 0


@pytest.mark.parametrize("result", [None, {}, {"cost": 0}])
def test_success_falls_back_to_estimate_only_when_result_cost_is_missing(
    session: Session, result: dict[str, Any] | None
) -> None:
    project = ProjectRepo(session).create_project("fallback")
    job = make_job(session, project, status=JobStatus.SUCCEEDED, attempts=1, result=result)
    expected = 0 if result and "cost" in result else 0.5
    assert reserved_cost(session, project.id) == expected
    charge(session, project, job, 1, expected)
    assert reserved_cost(session, project.id) == 0


@pytest.mark.parametrize("job_type", ["analyze_video", "write_script"])
def test_authoring_terminal_result_does_not_hold_cost_already_in_text_ledger(
    session: Session, job_type: str
) -> None:
    project = ProjectRepo(session).create_project("authoring")
    job = make_job(
        session,
        project,
        status=JobStatus.RUNNING,
        attempts=1,
        job_type=job_type,
        result={"cost": 9},
    )
    assert reserved_cost(session, project.id) == 0.5
    job.status = JobStatus.SUCCEEDED
    assert reserved_cost(session, project.id) == 0


def test_duplicate_malformed_and_future_paid_markers_do_not_invent_attempts(
    session: Session,
) -> None:
    project = ProjectRepo(session).create_project("markers")
    job = make_job(
        session,
        project,
        status=JobStatus.FAILED,
        attempts=2,
        markers=[1, 1, 2, 3, -1, 0, True, "2", None],
    )
    assert reserved_cost(session, project.id) == 1
    job.payload = {"estimated_cost": 0.5, "_started_paid_attempts": "1,2"}
    assert reserved_cost(session, project.id) == 0


def test_job_snapshot_does_not_hide_a_new_completion_from_reservations(session: Session) -> None:
    project = ProjectRepo(session).create_project("fresh values")
    job = make_job(session, project, status=JobStatus.RUNNING, attempts=1, markers=[1])
    session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status=JobStatus.SUCCEEDED, result={"cost": 0.7})
        .execution_options(synchronize_session=False)
    )
    assert job.status == JobStatus.RUNNING
    assert reserved_cost(session, project.id) == 0.7


@pytest.mark.parametrize("cost", [-1.0, float("inf"), float("nan")])
def test_invalid_estimate_cannot_lower_or_disable_budget_reservations(
    session: Session, cost: float
) -> None:
    project = ProjectRepo(session).create_project("invalid")
    make_job(session, project, estimate=cost)
    with pytest.raises(ValueError, match="finite and non-negative"):
        reserved_cost(session, project.id)
