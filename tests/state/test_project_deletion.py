from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

from avd.assets.media import MediaAsset  # noqa: F401  (registers the table)
from avd.director.mindmap import MindMapItem, MindMapNode, update
from avd.director.models import ChatMessage
from avd.jobs.models import Job, JobLane, JobStatus
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.deletion import ProjectBusy, delete_project
from avd.state.models import Node, Project, Version
from avd.state.repo import ProjectRepo


@pytest.fixture
def session(tmp_path: Path) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def _project(session: Session, tmp_path: Path, title: str) -> str:
    repo = ProjectRepo(session)
    project = repo.create_project(title, source_video_path=f"uploads/{title}.mp4")
    (tmp_path / "uploads").mkdir(exist_ok=True)
    (tmp_path / "uploads" / f"{title}.mp4").write_bytes(b"video")
    nodes = repo.create_shot_graph(project, 2)
    repo.record_version(nodes["script"], {"path": "x"})
    session.add(ChatMessage(project_id=project.id, message={"role": "user", "content": "hi"}))
    update(session, project, [MindMapItem(id="idea", label="想法")], [])
    session.add(Job(lane=JobLane.API, job_type="image_gen", payload={}, project_id=project.id))
    (tmp_path / "projects" / project.id).mkdir(parents=True)
    session.commit()
    return project.id


def test_delete_removes_everything_and_keeps_versions_guarded(
    session: Session, tmp_path: Path
) -> None:
    doomed = _project(session, tmp_path, "doomed")
    kept = _project(session, tmp_path, "kept")
    delete_project(session, tmp_path, doomed)
    session.commit()
    assert session.get(Project, doomed) is None
    for model in (Node, ChatMessage, MindMapNode, Job):
        count = session.scalar(
            select(func.count()).select_from(model).where(model.project_id == doomed)
        )
        assert count == 0
    assert not (tmp_path / "projects" / doomed).exists()
    assert not (tmp_path / "uploads" / "doomed.mp4").exists()
    assert session.get(Project, kept) is not None
    assert (tmp_path / "uploads" / "kept.mp4").exists()
    # The immutability guard is back for the remaining project's versions.
    with pytest.raises(DatabaseError, match="immutable"):
        session.execute(Version.__table__.delete())
        session.flush()


def test_running_job_blocks_deletion(session: Session, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path, "busy")
    job = session.scalar(select(Job).where(Job.project_id == project_id))
    assert job is not None
    job.status = JobStatus.RUNNING
    session.commit()
    with pytest.raises(ProjectBusy):
        delete_project(session, tmp_path, project_id)
