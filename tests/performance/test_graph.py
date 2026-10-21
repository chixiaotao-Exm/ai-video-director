from __future__ import annotations

import json
from copy import deepcopy

import pytest
from sqlalchemy import inspect, select

from avd.config import Settings
from avd.director.review import ReviewRequired, approve, require_review
from avd.engine.rebuild import JOB_TYPES, RebuildEngine
from avd.jobs import HandlerRegistry, JobQueue, Worker
from avd.jobs.models import JobStatus
from avd.performance.graph import configure_performance
from avd.performance.models import PerformanceConfig, PreparationRequest
from avd.performance.motion import MotionWorkflowSpec
from avd.performance.voice import VoiceModelImport, import_voice_model
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Version, Voice
from avd.state.repo import ProjectRepo


def imported_model(session, root, config):
    (root / "offline-test.pth").write_bytes(b"OFFLINE TEST CHECKPOINT, NOT A REAL MODEL")
    return import_voice_model(
        session,
        root,
        VoiceModelImport(
            voice_id=config.voice_id,
            model_path="offline-test.pth",
            sample_rate=40000,
            provenance="Explicit offline test fixture; never loaded for inference",
        ),
    ).id


@pytest.fixture
def performance(tmp_path):
    database = create_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    # Register approvals and the queue before creating tables.
    init_db(database)
    factory = create_session_factory(database)
    (tmp_path / "source.mp4").write_bytes(b"offline fake source")
    (tmp_path / "character.png").write_bytes(b"offline fake character")
    with factory() as session:
        character = Character(
            name="target", reference_images={"front": "character.png"}, consent_confirmed=True
        )
        voice = Voice(name="target voice", consent_confirmed=True)
        session.add_all([character, voice])
        session.flush()
        project = Project(title="performance", mode="performance_remix", budget_limit=None)
        session.add(project)
        session.flush()
        config = PerformanceConfig(
            preparation=PreparationRequest(source_video_path="source.mp4"),
            character_id=character.id,
            voice_id=voice.id,
            scene_prompt="Moonlit stage",
        )
        configure_performance(session, project, config, tmp_path)
        project_id = project.id
        session.commit()
    yield tmp_path, factory, project_id, config
    database.dispose()


def test_graph_has_real_independent_dependencies_and_no_tts(performance):
    root, factory, project_id, config = performance
    with factory() as session:
        project = session.get(Project, project_id)
        nodes = {n.kind.value: n for n in project.nodes}
        assert len(nodes) == 8
        assert "script" not in nodes and "voiceover" not in nodes
        assert {n.kind.value for n in nodes["motion_video"].depends_on} == {
            "performance_prepare",
            "scene_reference",
        }
        assert {n.kind.value for n in nodes["performance_sync"].depends_on} == {
            "performance_prepare",
            "motion_video",
            "singing_voice",
        }
        before = {n.id for n in project.nodes}
        configure_performance(session, project, config, root)
        assert {n.id for n in project.nodes} == before


def test_motion_workflow_snapshot_and_parameters_invalidate_only_video_branch(performance):
    root, factory, project_id, config = performance
    template = {
        "1": {
            "class_type": "OfflineMotion",
            "inputs": {
                "source": "{{motion_video}}",
                "reference": "{{reference_image}}",
                "seed": "{{seed}}",
            },
        },
    }
    path = root / "export.json"
    path.write_text(json.dumps(template), encoding="utf-8")
    spec = MotionWorkflowSpec(
        template_path=path,
        revision="offline-v1",
        model_versions={"offline-model": "fixture"},
        output_node_id="1",
        expected_frame_count=160,
        input_sampling={
            "frame_rate": "16/1",
            "frame_count": 160,
            "tail_padding_frames": 1,
            "output_tail_padding_frames": 1,
        },
    )
    selected = config.model_copy(update={"motion_workflow": spec, "motion_parameters": {"seed": 9}})
    profile = {"base_url": "http://127.0.0.1:8188", "timeout": 600}
    with factory() as session:
        project = session.get(Project, project_id)
        nodes = configure_performance(session, project, selected, root, comfyui_profile=profile)
        captured = deepcopy(nodes["motion_video"].recipe)
        assert captured["workflow"]["template"] == template
        assert captured["expected_frame_count"] == 160
        assert captured["workflow"]["spec"]["input_sampling"]["tail_padding_frames"] == 1
        assert captured["comfyui_profile"] == profile
        repo = ProjectRepo(session)
        for node in project.nodes:
            repo.record_version(node, {})
        historical = deepcopy(nodes["motion_video"].current_version.recipe_snapshot)
        changed = selected.model_copy(update={"motion_parameters": {"seed": 10}})
        configure_performance(session, project, changed, root, comfyui_profile=profile)
        assert {n.kind.value for n in project.nodes if n.status == NodeStatus.DIRTY} == {
            "motion_video",
            "performance_sync",
            "performance_final",
        }
        assert nodes["motion_video"].current_version.recipe_snapshot == historical
        for node in project.nodes:
            repo.record_version(node, {})
        template["1"]["inputs"]["strength"] = 0.5
        path.write_text(json.dumps(template), encoding="utf-8")
        configure_performance(session, project, changed, root, comfyui_profile=profile)
        assert (
            nodes["motion_video"].recipe["workflow"]["template_sha256"]
            != captured["workflow"]["template_sha256"]
        )
        assert nodes["singing_voice"].status == NodeStatus.CLEAN
        for node in project.nodes:
            repo.record_version(node, {})
        updated_spec = spec.model_copy(
            update={
                "expected_frame_count": 80,
                "input_sampling": type(spec.input_sampling)(frame_rate="8/1", frame_count=80),
            }
        )
        configure_performance(
            session,
            project,
            changed.model_copy(update={"motion_workflow": updated_spec}),
            root,
            comfyui_profile=profile,
        )
        assert nodes["motion_video"].recipe["expected_frame_count"] == 80
        assert {n.kind.value for n in project.nodes if n.status == NodeStatus.DIRTY} == {
            "motion_video",
            "performance_sync",
            "performance_final",
        }
        assert nodes["motion_video"].current_version.recipe_snapshot["expected_frame_count"] == 160


@pytest.mark.parametrize(
    "change,expected",
    [
        ("scene", {"scene_reference", "motion_video", "performance_sync", "performance_final"}),
        ("voice", {"singing_voice", "performance_mix", "performance_sync", "performance_final"}),
        (
            "interval",
            {
                "performance_prepare",
                "vocal_separation",
                "singing_voice",
                "motion_video",
                "performance_mix",
                "performance_sync",
                "performance_final",
            },
        ),
        ("none", set()),
    ],
)
def test_change_invalidates_only_true_descendants(performance, change, expected):
    root, factory, project_id, config = performance
    with factory() as session:
        project = session.get(Project, project_id)
        repo = ProjectRepo(session)
        for node in project.nodes:
            repo.record_version(node, {})
        snapshots = {v.id: deepcopy(v.recipe_snapshot) for v in session.scalars(select(Version))}
        updated = config.model_copy(deep=True)
        if change == "scene":
            updated.scene_prompt = "A forest stage"
        elif change == "voice":
            updated.voice_model_version_id = imported_model(session, root, config)
        elif change == "interval":
            updated.preparation.start_s = 1
        configure_performance(session, project, updated, root)
        assert {n.kind.value for n in project.nodes if n.status == NodeStatus.DIRTY} == expected
        assert {v.id: v.recipe_snapshot for v in session.scalars(select(Version))} == snapshots


def test_optional_lipsync_keeps_old_versions_when_toggled(performance):
    root, factory, project_id, config = performance
    with factory() as session:
        project = session.get(Project, project_id)
        enabled = config.model_copy(update={"lipsync": True})
        nodes = configure_performance(session, project, enabled, root)
        lip = nodes["performance_lipsync"]
        version = ProjectRepo(session).record_version(lip, {})
        nodes = configure_performance(session, project, config, root)
        assert session.get(Version, version.id) is version
        assert {n.kind for n in nodes["performance_final"].depends_on} == {
            NodeKind.PERFORMANCE_SYNC,
            NodeKind.PERFORMANCE_MIX,
        }


def test_performance_approvals_are_bound_to_inputs_and_output_versions(performance):
    root, factory, project_id, config = performance
    with factory() as session:
        project = session.get(Project, project_id)
        approve(session, project, "performance_inputs", "tester")
        require_review(session, project, "performance_inputs")
        with pytest.raises(ReviewRequired):
            approve(session, project, "performance_preview", "tester")
        repo = ProjectRepo(session)
        for node in project.nodes:
            # Only a completed video chain can be reviewed, not scene/voice references alone.
            repo.record_version(
                node,
                {"video": "offline-not-generated.mp4"}
                if node.kind == NodeKind.PERFORMANCE_FINAL
                else {},
            )
        approve(session, project, "performance_preview", "tester")
        changed = config.model_copy(update={"scene_prompt": "new scene"})
        configure_performance(session, project, changed, root)
        with pytest.raises(ReviewRequired):
            require_review(session, project, "performance_inputs")
        with pytest.raises(ReviewRequired):
            require_review(session, project, "performance_preview")


def test_offline_graph_runs_queue_cache_and_voice_only_rebuild(performance):
    root, factory, project_id, config = performance
    queue = JobQueue(factory)
    engine = RebuildEngine(factory, queue, Settings(_env_file=None, data_dir=root))
    registry = HandlerRegistry()
    calls = []

    def fake(job):
        calls.append(job.job_type)
        folder = root / job.payload["output_dir"] / f"attempt-{job.attempts}"
        folder.mkdir(parents=True)
        output = folder / "fake.txt"
        output.write_text("OFFLINE TEST OUTPUT, NOT MODEL MEDIA", encoding="utf-8")
        return {"outputs": {"test": output.relative_to(root).as_posix()}, "cost": 0}

    for _, job_type in JOB_TYPES.values():
        registry.register(job_type, fake)
    workers = [
        Worker(queue, lane, registry, on_finished=engine.on_finished)
        for lane in ("cpu", "gpu", "api")
    ]

    def drain():
        for _ in range(30):
            worked = [worker.run_once() for worker in workers]
            if not any(worked):
                break
        assert all(j.status == JobStatus.SUCCEEDED for j in queue.list())

    engine.rebuild(project_id)
    drain()
    assert len(calls) == 8
    assert engine.rebuild(project_id) == []
    with factory() as session:
        configure_performance(
            session,
            session.get(Project, project_id),
            config.model_copy(
                update={"voice_model_version_id": imported_model(session, root, config)}
            ),
            root,
        )
        session.commit()
    calls.clear()
    engine.rebuild(project_id)
    drain()
    assert set(calls) == {
        "singing_voice",
        "performance_mix",
        "performance_sync",
        "performance_final",
    }


def test_old_database_upgrade_preserves_version_rows(performance):
    root, factory, project_id, config = performance
    with factory() as session:
        project = ProjectRepo(session).create_project("legacy")
        legacy = ProjectRepo(session).add_node(project, NodeKind.SCRIPT, recipe={"line": "old"})
        version = ProjectRepo(session).record_version(legacy, {"legacy": "old.txt"})
        version_id, snapshot = version.id, deepcopy(version.recipe_snapshot)
        session.commit()
    database = factory.kw["bind"]
    with database.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE projects DROP COLUMN performance_config")
    init_db(database)
    init_db(database)
    assert "performance_config" in {c["name"] for c in inspect(database).get_columns("projects")}
    with factory() as session:
        assert session.get(Version, version_id).recipe_snapshot == snapshot
        assert session.get(Node, legacy.id).kind == NodeKind.SCRIPT
