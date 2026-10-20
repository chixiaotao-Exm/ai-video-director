"""Deleting a whole project: its graph, versions, jobs, chat and generated files."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from avd.jobs.models import Job, JobStatus
from avd.state.models import Project

_TRIGGER = (
    "CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN "
    "SELECT RAISE(ABORT, 'Version rows are immutable'); END"
)


class ProjectBusy(RuntimeError):
    """A job of this project is running; deleting now would orphan its process."""


def delete_project(session: Session, data_dir: Path, project_id: str) -> None:
    """Remove the project and everything that belongs only to it.

    Versions are immutable during a project's life, so the guard trigger is lifted
    and restored inside the same transaction (SQLite holds the write lock meanwhile).
    Media-library clips made from the project are kept, just unlinked.
    """
    from avd.assets.media import MediaAsset

    sqlite = session.get_bind().dialect.name == "sqlite"
    if sqlite:
        connection = session.connection()
        # SQLAlchemy may have autobegun a Session transaction even though sqlite3
        # has not started one for SELECTs. Conversely a caller's write/savepoint
        # may already own a real transaction, which must not be restarted.
        driver_connection = connection.connection.driver_connection
        assert driver_connection is not None
        if driver_connection.in_transaction:
            # Upgrade a deferred transaction before reading job status. A stale
            # SQLite snapshot must fail here rather than delete a newly claimed job.
            connection.execute(
                text("UPDATE projects SET id = id WHERE id = :id"), {"id": project_id}
            )
        else:
            connection.execute(text("BEGIN IMMEDIATE"))

    project = session.get(Project, project_id)
    if project is None:
        raise KeyError("Project not found")
    running = session.scalar(
        select(Job.id).where(Job.project_id == project_id, Job.status == JobStatus.RUNNING)
    )
    if running is not None:
        raise ProjectBusy("A job of this project is still running; wait for it or cancel it first")
    source = project.source_video_path
    session.execute(delete(Job).where(Job.project_id == project_id))
    session.execute(
        update(MediaAsset).where(MediaAsset.project_id == project_id).values(project_id=None)
    )
    session.flush()
    session.expunge_all()
    if sqlite:
        session.execute(text("PRAGMA foreign_keys=ON"))
        session.execute(text("DROP TRIGGER IF EXISTS versions_no_delete"))
    # Clear version pointers first so cascaded version deletes don't trip FK order.
    session.execute(
        text("UPDATE nodes SET current_version_id = NULL WHERE project_id = :id"),
        {"id": project_id},
    )
    session.execute(
        text("DELETE FROM versions WHERE node_id IN (SELECT id FROM nodes WHERE project_id = :id)"),
        {"id": project_id},
    )
    session.execute(delete(Project).where(Project.id == project_id))
    if sqlite:
        session.execute(text(_TRIGGER))
    session.flush()

    root = data_dir.expanduser().resolve()
    folder = (root / "projects" / project_id).resolve()
    if folder.is_relative_to(root / "projects") and folder != root / "projects":
        shutil.rmtree(folder, ignore_errors=True)
    if source:
        still_used = session.scalar(
            select(Project.id).where(Project.source_video_path == source)
        ) or session.scalar(select(MediaAsset.id).where(MediaAsset.source == source))
        upload = (root / source).resolve()
        if not still_used and upload.is_relative_to(root / "uploads"):
            upload.unlink(missing_ok=True)


__all__ = ["ProjectBusy", "delete_project"]
