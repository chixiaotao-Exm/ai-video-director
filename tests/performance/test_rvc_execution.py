"""Runtime provenance must survive queuing and invalidate stale singing caches."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

import pytest

from avd.config import Settings
from avd.engine.rebuild import RebuildEngine
from avd.jobs import JobQueue
from avd.performance.backend import LocalPerformanceBackend
from avd.performance.voice import RVCInstallation, SingingVoiceError, rvc_installation_snapshot
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def runtime_graph(tmp_path: Path):
    cli = tmp_path / "rvc" / "infer" / "cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# offline runtime source v1", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        rvc_installation=RVCInstallation(
            project_root=cli.parents[1],
            python_executable=Path(sys.executable),
            revision="offline-fixture-v1",
        ),
    )
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    queue = JobQueue(sessions)
    engine = RebuildEngine(sessions, queue, settings)
    with sessions() as session:
        project = Project(
            title="runtime cache fixture",
            mode="performance_remix",
            voice=Voice(name="consented target", consent_confirmed=True),
            budget_limit=None,
        )
        session.add(project)
        session.flush()
        repo = ProjectRepo(session)
        singing = repo.add_node(project, NodeKind.SINGING_VOICE)
        mix = repo.add_node(project, NodeKind.PERFORMANCE_MIX, depends_on=[singing])
        final = repo.add_node(project, NodeKind.PERFORMANCE_FINAL, depends_on=[mix])
        unrelated = repo.add_node(project, NodeKind.PERFORMANCE_PREPARE)
        for node in (singing, mix, final, unrelated):
            path = tmp_path / f"{node.kind.value}.txt"
            path.write_text("OFFLINE CACHE FIXTURE; NOT MODEL MEDIA", encoding="utf-8")
            node.recipe = engine._effective_recipe(node)
            repo.record_version(node, {"fixture": path.name})
        ids = {node.kind: node.id for node in project.nodes}
        project_id = project.id
        session.commit()
    try:
        yield settings, sessions, queue, engine, project_id, ids
    finally:
        database.dispose()


@pytest.mark.parametrize("change", ["source", "configuration"])
def test_runtime_change_invalidates_clean_singing_and_only_its_descendants(runtime_graph, change):
    settings, sessions, queue, engine, project_id, ids = runtime_graph
    assert engine.rebuild(project_id) == []
    with sessions() as session:
        old = deepcopy(
            session.get(Node, ids[NodeKind.SINGING_VOICE]).current_version.recipe_snapshot
        )
    if change == "source":
        settings.rvc_installation.cli().write_text("# offline source v2", encoding="utf-8")
    else:
        settings.rvc_installation.revision = "offline-fixture-v2"
    [job_id] = engine.rebuild(project_id)
    job = queue.get(job_id)
    assert job.node_id == ids[NodeKind.SINGING_VOICE]
    assert job.payload["recipe"]["_execution"] != old["_execution"]
    with sessions() as session:
        singing = session.get(Node, ids[NodeKind.SINGING_VOICE])
        assert singing.status == NodeStatus.RUNNING
        assert singing.current_version.recipe_snapshot == old
        for kind in (NodeKind.PERFORMANCE_MIX, NodeKind.PERFORMANCE_FINAL):
            assert session.get(Node, ids[kind]).status == NodeStatus.DIRTY
        assert session.get(Node, ids[NodeKind.PERFORMANCE_PREPARE]).status == NodeStatus.CLEAN


def test_queued_singing_keeps_runtime_snapshot_and_refuses_changed_sources(runtime_graph):
    settings, sessions, queue, engine, project_id, ids = runtime_graph
    with sessions() as session:
        singing = session.get(Node, ids[NodeKind.SINGING_VOICE])
        ProjectRepo(session).update_recipe(singing, {"voice_model_version_id": "fixture-id"})
        session.commit()
    [job_id] = engine.rebuild(project_id)
    before = deepcopy(queue.get(job_id).payload["recipe"])
    settings.rvc_installation.cli().write_text("# changed after queue submission", encoding="utf-8")
    assert engine.rebuild(project_id) == []
    assert queue.get(job_id).payload["recipe"] == before
    with pytest.raises(SingingVoiceError, match="source files changed"):
        LocalPerformanceBackend(settings, sessions).run(
            "singing_voice",
            data_dir=settings.data_dir,
            recipe=before,
            inputs=[],
            context={},
            output_dir=settings.data_dir / "unused-output",
        )
    assert not (settings.data_dir / "unused-output").exists()


@pytest.mark.parametrize("snapshot_kind", ["missing", "not_ready"])
def test_singing_backend_requires_a_ready_frozen_runtime(runtime_graph, snapshot_kind):
    settings, sessions, _, _, _, _ = runtime_graph
    execution = (
        {"rvc_runtime": rvc_installation_snapshot(None)} if snapshot_kind == "not_ready" else {}
    )
    with pytest.raises(SingingVoiceError, match="recipe"):
        LocalPerformanceBackend(settings, sessions).run(
            "singing_voice",
            data_dir=settings.data_dir,
            recipe={"voice_model_version_id": "fixture-id", "_execution": execution},
            inputs=[],
            context={},
            output_dir=settings.data_dir / "unused-output",
        )
