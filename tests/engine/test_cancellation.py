"""Explicit cancellation must survive automatic dependency scheduling."""

from pathlib import Path

import pytest

from avd.config import Settings
from avd.engine import RebuildEngine
from avd.jobs import JobQueue, JobStatus
from avd.performance.voice import RVCInstallation, rvc_installation_snapshot
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project
from avd.state.repo import ProjectRepo


@pytest.mark.parametrize("kind", [NodeKind.SHOT_FRAME, NodeKind.SCENE_REFERENCE])
def test_sibling_completion_cannot_restart_cancelled_work(tmp_path: Path, kind: NodeKind) -> None:
    settings = Settings(data_dir=tmp_path, confirm_threshold=10, _env_file=None)
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    queue = JobQueue(sessions)
    engine = RebuildEngine(sessions, queue, settings)
    with sessions() as session:
        project = Project(
            title="Cancellation", character=Character(name="Target", consent_confirmed=True)
        )
        session.add(project)
        session.flush()
        repo = ProjectRepo(session)
        cancelled = repo.add_node(project, kind, recipe={"prompt": "target"})
        sibling = repo.add_node(project, kind, recipe={"prompt": "other"})
        downstream = repo.add_node(project, NodeKind.FINAL, depends_on=[cancelled, sibling])
        session.commit()
        project_id, node_id, sibling_id, final_id = (
            project.id,
            cancelled.id,
            sibling.id,
            downstream.id,
        )

    initial = engine.rebuild(project_id)
    cancelled_job = next(
        queue.get(job_id) for job_id in initial if queue.get(job_id).node_id == node_id
    )
    assert queue.cancel(cancelled_job.id)
    claimed = queue.claim_next("api")
    assert claimed is not None and claimed.node_id == sibling_id
    output = tmp_path / claimed.payload["output_dir"] / "image.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"explicit offline fake image")
    finished = queue.complete(
        claimed.id, {"outputs": {"image": output.relative_to(tmp_path).as_posix()}}
    )

    # Recreate the engine to prove cancellation is durable rather than process state.
    restarted = RebuildEngine(sessions, queue, settings)
    restarted.on_finished(finished)
    restarted.on_finished(finished)
    assert not queue.list(project_id, JobStatus.QUEUED)
    with sessions() as session:
        assert session.get(Node, node_id).status == NodeStatus.DIRTY
        assert session.get(Node, sibling_id).status == NodeStatus.CLEAN
        assert session.get(Node, final_id).status == NodeStatus.DIRTY

    # A deliberate rebuild is still the user's way to resume the cancelled node.
    [resumed_id] = restarted.rebuild(project_id)
    assert queue.get(resumed_id).node_id == node_id
    assert resumed_id != cancelled_job.id
    database.dispose()


@pytest.mark.parametrize("global_ai_label", [False, True])
def test_singing_and_final_execution_settings_are_frozen(
    tmp_path: Path, global_ai_label: bool
) -> None:
    settings = Settings(
        data_dir=tmp_path, ai_label=global_ai_label, rvc_installation=None, _env_file=None
    )
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    engine = RebuildEngine(sessions, JobQueue(sessions), settings)
    assert engine._execution_settings(Node(kind=NodeKind.SINGING_VOICE)) == {
        "rvc_runtime": rvc_installation_snapshot(None)
    }
    assert engine._execution_settings(Node(kind=NodeKind.PERFORMANCE_FINAL)) == {"ai_label": True}
    settings.rvc_installation = RVCInstallation(
        project_root=tmp_path / "rvc",
        python_executable=tmp_path / "python.exe",
        revision="review-v1",
    )
    frozen = engine._execution_settings(Node(kind=NodeKind.SINGING_VOICE))
    settings.rvc_installation.revision = "review-v2"
    assert frozen["rvc_runtime"]["installation"]["revision"] == "review-v1"
    assert frozen["rvc_runtime"]["not_ready"] is True
    database.dispose()
