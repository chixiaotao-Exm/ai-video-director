"""Mandatory performance labelling must invalidate only a legacy unlabelled final."""

from copy import deepcopy
from pathlib import Path

import pytest

from avd.config import Settings
from avd.engine import RebuildEngine
from avd.jobs import Job, JobQueue, JobStatus
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, NodeStatus, Project
from avd.state.repo import ProjectRepo


@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("legacy_execution", [None, {}, {"ai_label": False}])
def test_rebuild_replaces_only_unlabelled_final_and_preserves_immutable_history(
    tmp_path: Path,
    failed: bool,
    legacy_execution: dict | None,
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, ai_label=False)
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    queue = JobQueue(sessions)
    engine = RebuildEngine(sessions, queue, settings)
    try:
        with sessions() as session:
            project = Project(title="Offline legacy performance", mode="performance_remix")
            session.add(project)
            session.flush()
            repo = ProjectRepo(session)
            prepared = repo.add_node(project, NodeKind.PERFORMANCE_PREPARE)
            synced = repo.add_node(project, NodeKind.PERFORMANCE_SYNC, depends_on=[prepared])
            mixed = repo.add_node(project, NodeKind.PERFORMANCE_MIX, depends_on=[prepared])
            final = repo.add_node(project, NodeKind.PERFORMANCE_FINAL, depends_on=[synced, mixed])
            old_outputs = {}
            for node in (prepared, synced, mixed, final):
                output = tmp_path / f"{node.kind.value}.txt"
                output.write_text("OFFLINE CACHE FIXTURE; NOT MODEL MEDIA", encoding="utf-8")
                node.recipe = engine._effective_recipe(node)
                if node is final:
                    if legacy_execution is None:
                        node.recipe.pop("_execution")
                    else:
                        node.recipe["_execution"] = deepcopy(legacy_execution)
                repo.record_version(node, {"fixture": output.name})
                old_outputs[node.id] = (node.current_version_id, output.read_bytes())
            project_id, final_id = project.id, final.id
            old_final_recipe = deepcopy(final.current_version.recipe_snapshot)
            failed_id = None
            failed_payload = {"recipe": old_final_recipe, "recipe_hash": "legacy-job-history"}
            if failed:
                final.status = NodeStatus.FAILED
                historical_job = Job(
                    project_id=project_id,
                    node_id=final_id,
                    lane="cpu",
                    job_type="performance_final",
                    status=JobStatus.FAILED,
                    payload=deepcopy(failed_payload),
                    attempts=1,
                    max_attempts=1,
                    error="Legacy final did not freeze an AI label",
                    result={"_engine_handled": True},
                )
                session.add(historical_job)
                session.flush()
                failed_id = historical_job.id
            session.commit()
        if failed:
            # A sibling completion is not an explicit request to retry a failed export.
            assert engine.rebuild(project_id, _automatic=True) == []
            with sessions() as session:
                untouched = session.get(Node, final_id)
                assert untouched.status == NodeStatus.FAILED
                assert untouched.recipe == old_final_recipe
        [job_id] = engine.rebuild(project_id)
        queued = queue.get(job_id)
        assert queued.node_id == final_id
        assert queued.job_type == "performance_final"
        assert queued.lane == "cpu"
        assert queued.payload["recipe"]["_execution"] == {"ai_label": True}
        with sessions() as session:
            project = session.get(Project, project_id)
            for node in project.nodes:
                old_version, old_bytes = old_outputs[node.id]
                assert node.current_version_id == old_version
                output = next(iter(node.current_version.output_paths.values()))
                assert (tmp_path / output).read_bytes() == old_bytes
                assert node.status == (
                    NodeStatus.RUNNING if node.id == final_id else NodeStatus.CLEAN
                )
            final = session.get(Node, final_id)
            assert final.current_version.recipe_snapshot == old_final_recipe
        if failed_id is not None:
            old_job = queue.get(failed_id)
            assert old_job.status == JobStatus.FAILED
            assert old_job.payload == failed_payload
            assert old_job.attempts == old_job.max_attempts == 1
            assert old_job.error == "Legacy final did not freeze an AI label"
            assert old_job.result == {"_engine_handled": True}
        assert engine.rebuild(project_id) == []
    finally:
        database.dispose()


@pytest.mark.parametrize("execution", [{"ai_label": True}, {"ai_label": True, "extra": 1}])
def test_rebuild_does_not_retry_a_labelled_final_failure(tmp_path: Path, execution: dict) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, ai_label=False)
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    queue = JobQueue(sessions)
    engine = RebuildEngine(sessions, queue, settings)
    try:
        with sessions() as session:
            project = Project(title="A genuine export failure", mode="performance_remix")
            session.add(project)
            session.flush()
            final = ProjectRepo(session).add_node(
                project, NodeKind.PERFORMANCE_FINAL, recipe={"_execution": execution}
            )
            final.status = NodeStatus.FAILED
            project_id, final_id = project.id, final.id
            session.commit()
        assert engine.rebuild(project_id) == []
        assert queue.list(project_id) == []
        with sessions() as session:
            final = session.get(Node, final_id)
            assert final.status == NodeStatus.FAILED
            assert final.recipe == {"_execution": execution}
    finally:
        database.dispose()
