"""A worker must not claim a job between the deletion check and its writes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from avd.assets.media import MediaAsset  # noqa: F401 -- registers the referenced table
from avd.jobs.models import Job, JobStatus
from avd.jobs.queue import JobQueue
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.deletion import ProjectBusy, delete_project
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'delete.db').as_posix()}", connect_args={"timeout": 0.05}
    )
    init_db(engine)
    yield create_session_factory(engine)
    engine.dispose()


def seed(factory: sessionmaker[Session]) -> tuple[str, str, str]:
    with factory() as session:
        project = ProjectRepo(session).create_project("Delete target")
        kept = ProjectRepo(session).create_project("Keep unchanged")
        job_id = JobQueue(factory).submit(
            "api", "offline", {}, project_id=project.id, session=session
        )
        session.commit()
        return project.id, kept.id, job_id


def begin_caller_transaction(session: Session, kind: str, kept_id: str) -> None:
    if kind == "read":
        assert session.get(Project, kept_id) is not None
        assert session.in_transaction()
    elif kind in {"deferred", "immediate"}:
        session.execute(text("BEGIN IMMEDIATE" if kind == "immediate" else "BEGIN"))
        assert session.get(Project, kept_id) is not None
    elif kind == "write":
        kept = session.get(Project, kept_id)
        assert kept is not None
        kept.title = "Uncommitted caller edit"
        session.flush()


@pytest.mark.parametrize("transaction", ["fresh", "read", "deferred", "immediate", "write"])
def test_claim_is_blocked_after_running_check_until_delete_commits(
    factory: sessionmaker[Session],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transaction: str,
) -> None:
    project_id, kept_id, job_id = seed(factory)
    queue = JobQueue(factory)
    attempted: list[Job | None] = []
    lock_errors: list[OperationalError] = []
    with factory() as session:
        begin_caller_transaction(session, transaction, kept_id)
        scalar = session.scalar
        checked = False

        def claim_after_check(statement: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal checked
            result = scalar(statement, *args, **kwargs)
            if not checked and "jobs.status" in str(statement):
                checked = True
                assert result is None
                # A second connection reproduces the former check/claim/delete
                # interleaving. With the deletion lock held it must fail promptly.
                try:
                    attempted.append(queue.claim_next("api"))
                except OperationalError as exc:
                    lock_errors.append(exc)
            return result

        monkeypatch.setattr(session, "scalar", claim_after_check)
        delete_project(session, tmp_path, project_id)
        assert checked
        assert attempted == []
        assert len(lock_errors) == 1 and "locked" in str(lock_errors[0]).lower()
        session.commit()
    assert queue.claim_next("api") is None
    assert queue.get(job_id) is None
    with factory() as session:
        assert session.get(Project, project_id) is None
        assert session.get(Project, kept_id) is not None


def test_existing_identity_map_does_not_hide_a_worker_that_claimed_first(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    project_id, _, job_id = seed(factory)
    queue = JobQueue(factory)
    with factory() as session:
        assert session.get(Project, project_id) is not None
        claimed = queue.claim_next("api")
        assert claimed is not None and claimed.id == job_id
        with pytest.raises(ProjectBusy, match="still running"):
            delete_project(session, tmp_path, project_id)
        session.rollback()
    assert queue.get(job_id).status == JobStatus.RUNNING
    with factory() as session:
        assert session.get(Project, project_id) is not None


@pytest.mark.parametrize("transaction", ["read", "deferred", "immediate", "write"])
def test_delete_keeps_caller_transaction_and_can_be_rolled_back(
    factory: sessionmaker[Session], tmp_path: Path, transaction: str
) -> None:
    project_id, kept_id, job_id = seed(factory)
    with factory() as session:
        begin_caller_transaction(session, transaction, kept_id)
        outer = session.get_transaction()
        delete_project(session, tmp_path, project_id)
        if outer is not None:
            assert session.get_transaction() is outer
        assert session.get(Project, project_id) is None
        session.rollback()
    with factory() as session:
        assert session.get(Project, project_id) is not None
        assert session.get(Job, job_id).status == JobStatus.QUEUED
        assert session.get(Project, kept_id).title == "Keep unchanged"


def test_delete_works_inside_an_existing_savepoint_without_committing_it(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    project_id, _, job_id = seed(factory)
    with factory() as session:
        session.execute(text("BEGIN"))
        nested = session.begin_nested()
        delete_project(session, tmp_path, project_id)
        assert nested.is_active
        nested.rollback()
        assert session.get(Project, project_id) is not None
        assert session.get(Job, job_id).status == JobStatus.QUEUED
        session.rollback()


def test_stale_wal_read_transaction_cannot_delete_a_newly_claimed_job(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    project_id, _, job_id = seed(factory)
    with factory() as session:
        assert session.scalar(text("PRAGMA journal_mode=WAL")) == "wal"
        session.commit()
    queue = JobQueue(factory)
    with factory() as session:
        session.execute(text("BEGIN"))
        assert session.get(Project, project_id) is not None
        # WAL permits the worker to commit while this caller retains a stale
        # read snapshot. Upgrading must fail without restarting that snapshot.
        claimed = queue.claim_next("api")
        assert claimed is not None and claimed.id == job_id
        outer = session.get_transaction()
        with pytest.raises(OperationalError, match="locked"):
            delete_project(session, tmp_path, project_id)
        assert session.get_transaction() is outer
        session.rollback()
    assert queue.get(job_id).status == JobStatus.RUNNING
    with factory() as session:
        assert session.get(Project, project_id) is not None
