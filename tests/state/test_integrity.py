from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy

import pytest
from sqlalchemy import delete, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from avd.state.db import create_engine, create_session_factory, init_db, upgrade_sqlite_schema
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Version, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def repo() -> Iterator[ProjectRepo]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as session:
        yield ProjectRepo(session)
    engine.dispose()


@pytest.mark.parametrize("target", ["version", "node", "project"])
def test_orm_deletion_preserves_versions(repo: ProjectRepo, target: str) -> None:
    project = repo.create_project("Immutable history")
    node = repo.add_node(project, NodeKind.SHOT_FRAME)
    version = repo.record_version(node, {"image": "projects/original.png"})
    repo.session.commit()
    repo.session.delete({"version": version, "node": node, "project": project}[target])
    with pytest.raises(ValueError, match="immutable"):
        repo.session.flush()
    repo.session.rollback()
    assert repo.session.get(Version, version.id) is version
    assert version.output_paths == {"image": "projects/original.png"}


@pytest.mark.parametrize("action", ["update", "version", "node", "project"])
def test_sqlite_bulk_changes_cannot_mutate_version_history(repo: ProjectRepo, action: str) -> None:
    project = repo.create_project("Bulk protection")
    node = repo.add_node(project, NodeKind.SCRIPT)
    version = repo.record_version(node, {}, note="Retain me")
    repo.session.commit()
    statements = {
        "update": update(Version).values(note="Overwritten"),
        "version": delete(Version),
        "node": delete(Node),
        "project": delete(Project),
    }
    with pytest.raises(IntegrityError, match="immutable"):
        repo.session.execute(statements[action])
    repo.session.rollback()
    assert repo.session.get(Version, version.id).note == "Retain me"
    assert repo.session.scalar(text("PRAGMA foreign_keys")) == 1


UNSAFE_PATHS = [
    "../escape.wav",
    "nested/../escape.wav",
    r"nested\..\escape.wav",
    "/absolute.wav",
    r"\absolute.wav",
    r"C:\absolute.wav",
    "C:relative.wav",
    r"\\server\share\sample.wav",
    "file.wav:stream",
    "\x00bad.wav",
]


@pytest.mark.parametrize("path", UNSAFE_PATHS)
@pytest.mark.parametrize("field", ["source", "lora", "reference", "model", "sample", "output"])
def test_persisted_media_paths_reject_both_platforms(
    repo: ProjectRepo, path: str, field: str
) -> None:
    if field == "source":
        item = Project(title="Unsafe", source_video_path=path)
    elif field == "lora":
        item = Character(name="Unsafe", lora_path=path)
    elif field == "reference":
        item = Character(name="Unsafe", reference_images={"front": path})
    elif field == "model":
        item = Voice(name="Unsafe", model_path=path)
    elif field == "sample":
        item = Voice(name="Unsafe", sample_audio_paths=[path])
    else:
        project = repo.create_project("Unsafe output")
        node = repo.add_node(project, NodeKind.SHOT_FRAME)
        item = Version(
            node_id=node.id, number=1, recipe_hash="a" * 64, output_paths={"image": path}
        )
    repo.session.add(item)
    with pytest.raises(ValueError, match="relative path"):
        repo.session.flush()


def test_relative_windows_paths_are_normalized(repo: ProjectRepo) -> None:
    character = Character(
        name="Ada",
        reference_images={"front": r"refs\front.png"},
        lora_path=r"models\character.safetensors",
    )
    voice = Voice(
        name="Ada voice", sample_audio_paths=[r"voice\sample.wav"], model_path=r"models\voice.bin"
    )
    repo.session.add_all([character, voice])
    project = repo.create_project("Portable", source_video_path=r"uploads\source.mp4")
    node = repo.add_node(project, NodeKind.SHOT_FRAME)
    version = repo.record_version(node, [r"projects\frame.png"])
    repo.session.commit()
    assert project.source_video_path == "uploads/source.mp4"
    assert character.reference_images == {"front": "refs/front.png"}
    assert character.lora_path == "models/character.safetensors"
    assert voice.sample_audio_paths == ["voice/sample.wav"]
    assert voice.model_path == "models/voice.bin"
    assert version.output_paths == ["projects/frame.png"]


def _storyboard() -> dict:
    return {
        "title": "A story",
        "shots": [
            {
                "index": index,
                "line": f"Line {index}",
                "emotion": "calm",
                "visual_prompt": f"Scene {index}",
                "camera": "static",
                "duration_s": 3.0,
            }
            for index in (1, 2)
        ],
    }


def _materialized_graph(repo: ProjectRepo) -> tuple[Project, dict]:
    project = repo.create_project("Restoration")
    graph = repo.create_shot_graph(project, 2)
    script = graph["script"]
    script.recipe = _storyboard()
    for index, shot in enumerate(script.recipe["shots"]):
        graph["shot_frame"][index].recipe = {
            "visual_prompt": shot["visual_prompt"],
            "character_id": "retained-character",
        }
        graph["shot_video"][index].recipe = {key: shot[key] for key in ("camera", "duration_s")}
        graph["voiceover"][index].recipe = {key: shot[key] for key in ("line", "emotion")}
    for node in project.nodes:
        repo.record_version(node, {})
    repo.session.commit()
    return project, graph


def test_reverting_script_restores_materialized_shot_fields(repo: ProjectRepo) -> None:
    project, graph = _materialized_graph(repo)
    original = deepcopy(graph["script"].recipe)
    graph["script"].recipe = {
        "title": "Changed",
        "shots": [
            {
                **shot,
                "line": "Changed line",
                "emotion": "happy",
                "visual_prompt": "Changed scene",
                "camera": "pan",
                "duration_s": 5.0,
            }
            for shot in original["shots"]
        ],
    }
    for node in project.nodes:
        if node.kind == NodeKind.SHOT_FRAME:
            node.recipe = {**node.recipe, "visual_prompt": "Changed scene"}
        elif node.kind == NodeKind.SHOT_VIDEO:
            node.recipe = {"camera": "pan", "duration_s": 5.0}
        elif node.kind == NodeKind.VOICEOVER:
            node.recipe = {"line": "Changed line", "emotion": "happy"}
        repo.record_version(node, {})
    before = list(repo.session.scalars(select(Version.id)))
    repo.revert(graph["script"], 1)
    assert graph["script"].recipe == original
    for index, shot in enumerate(original["shots"]):
        assert graph["shot_frame"][index].recipe == {
            "visual_prompt": shot["visual_prompt"],
            "character_id": "retained-character",
        }
        assert graph["shot_video"][index].recipe == {"camera": "static", "duration_s": 3.0}
        assert graph["voiceover"][index].recipe == {"line": shot["line"], "emotion": "calm"}
    assert all(
        node.status == NodeStatus.DIRTY for node in project.nodes if node is not graph["script"]
    )
    assert list(repo.session.scalars(select(Version.id))) == before


def test_voiceover_revert_versions_readable_script_without_dirtying_other_shots(
    repo: ProjectRepo,
) -> None:
    project, graph = _materialized_graph(repo)
    target = graph["voiceover"][1]
    target.recipe = {"line": "Changed", "emotion": "excited"}
    repo.record_version(target, {})
    changed = deepcopy(graph["script"].recipe)
    changed["shots"][1].update(line="Changed", emotion="excited")
    graph["script"].recipe = changed
    changed_script_version = repo.record_version(graph["script"], {})
    repo.revert(target, 1)
    script = graph["script"]
    assert script.recipe["shots"][1]["line"] == "Line 2"
    assert script.recipe["shots"][1]["emotion"] == "calm"
    assert script.current_version.recipe_snapshot == script.recipe
    assert script.current_version_id != changed_script_version.id
    assert changed_script_version.recipe_snapshot == changed
    assert {node for node in project.nodes if node.status == NodeStatus.DIRTY} == {
        graph["lipsync"][1],
        graph["subtitles"][1],
        graph["final"],
    }


def test_analysis_revert_restores_template_or_clears_legacy_result(repo: ProjectRepo) -> None:
    project = repo.create_project("Analysis")
    node = repo.add_node(project, NodeKind.ANALYSIS, recipe={"source_video_path": "source.mp4"})
    repo.record_version(node, {})
    node.recipe = {"template": {"hook": "Original"}}
    repo.record_version(node, {})
    node.recipe = {"template": {"hook": "Changed"}}
    repo.record_version(node, {})
    project.analysis_result = {"hook": "Changed"}
    repo.revert(node, 2)
    assert project.analysis_result == {"hook": "Original"}
    repo.revert(node, 1)
    assert project.analysis_result is None
    assert len(repo.list_versions(node)) == 3


def test_existing_sqlite_schema_additions_are_idempotent_and_preserve_rows() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE characters (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE voices (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE jobs (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE cost_entries (id TEXT PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO jobs (id) VALUES ('existing')")
    upgrade_sqlite_schema(engine)
    upgrade_sqlite_schema(engine)
    inspector = inspect(engine)
    assert {column["name"] for column in inspector.get_columns("jobs")} == {
        "id",
        "claim_token",
        "lease_expires_at",
    }
    assert {column["name"] for column in inspector.get_columns("characters")} >= {
        "consent_confirmed_at",
        "consent_confirmed_by",
    }
    with Session(engine) as session:
        assert session.scalar(text("SELECT id FROM jobs")) == "existing"
        session.execute(text("INSERT INTO cost_entries VALUES ('first', 'existing', 1)"))
        with pytest.raises(IntegrityError):
            session.execute(text("INSERT INTO cost_entries VALUES ('second', 'existing', 1)"))
    engine.dispose()
