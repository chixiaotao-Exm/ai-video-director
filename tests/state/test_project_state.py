from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, NodeStatus, Version
from avd.state.repo import ProjectRepo


@pytest.fixture
def repo_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def repo(repo_session):
    return ProjectRepo(repo_session)


@pytest.fixture
def graph(repo):
    project = repo.create_project("demo", source_video_path="source.mp4")
    nodes = repo.create_shot_graph(project, 3)
    for node in project.nodes:
        repo.record_version(node, {})
    repo.session.commit()
    return repo, project, nodes


def _nodes(project, kind):
    return [node for node in project.nodes if node.kind == kind]


def test_graph_shape_for_three_shots(graph):
    repo, project, nodes = graph

    assert len(_nodes(project, NodeKind.SCRIPT)) == 1
    for kind in (
        NodeKind.SHOT_FRAME,
        NodeKind.SHOT_VIDEO,
        NodeKind.VOICEOVER,
        NodeKind.LIPSYNC,
        NodeKind.SUBTITLES,
    ):
        assert len(_nodes(project, kind)) == 3
    assert len(_nodes(project, NodeKind.FINAL)) == 1

    script = nodes["script"]
    final = nodes["final"]
    assert script.depends_on == []
    assert {node.kind for node in script.dependents} == {
        NodeKind.SHOT_FRAME,
        NodeKind.VOICEOVER,
    }
    assert len(final.depends_on) == 3
    assert all(dependency.kind == NodeKind.SUBTITLES for dependency in final.depends_on)

    for index in range(3):
        frame = nodes["shot_frame"][index]
        video = nodes["shot_video"][index]
        voiceover = nodes["voiceover"][index]
        lipsync = nodes["lipsync"][index]
        subtitles = nodes["subtitles"][index]
        assert frame.depends_on == [script]
        assert video.depends_on == [frame]
        assert voiceover.depends_on == [script]
        assert set(lipsync.depends_on) == {video, voiceover}
        assert subtitles in lipsync.dependents
        assert subtitles.depends_on == [lipsync]


def test_new_graph_plan_contains_all_nodes_in_topological_order(repo):
    project = repo.create_project("fresh")
    nodes = repo.create_shot_graph(project, 3)

    plan = repo.plan_rebuild(project)

    assert len(plan) == 17
    assert plan[0] is nodes["script"]
    assert plan[-1] is nodes["final"]
    positions = {node.id: index for index, node in enumerate(plan)}
    for node in plan:
        for dependency in node.depends_on:
            assert positions[dependency.id] < positions[node.id]


def test_editing_voiceover_shot_two_only_dirties_its_downstream(graph):
    repo, project, nodes = graph
    target = nodes["voiceover"][1]

    repo.update_recipe(target, {"text": "changed"}, note="user edit")
    repo.session.commit()

    dirty = {node for node in project.nodes if node.status == NodeStatus.DIRTY}
    assert dirty == {
        target,
        nodes["lipsync"][1],
        nodes["subtitles"][1],
        nodes["final"],
    }
    untouched = set(project.nodes) - dirty
    assert nodes["voiceover"][0] in untouched
    assert nodes["voiceover"][2] in untouched
    assert nodes["lipsync"][0] in untouched
    assert nodes["lipsync"][2] in untouched


def test_editing_script_dirties_everything_downstream(graph):
    repo, project, nodes = graph
    repo.update_recipe(nodes["script"], {"text": "new script"})
    repo.session.commit()

    dirty = {node for node in project.nodes if node.status == NodeStatus.DIRTY}
    assert dirty == set(project.nodes)


def test_plan_rebuild_is_topological_and_deterministic(graph):
    repo, project, nodes = graph
    repo.update_recipe(nodes["script"], {"text": "new script"})

    plan = repo.plan_rebuild(project)
    positions = {node.id: index for index, node in enumerate(plan)}
    assert len(plan) == len(project.nodes)
    for node in plan:
        for dependency in node.depends_on:
            assert positions[dependency.id] < positions[node.id]

    # A deterministic kind/shot tie-break places the shared script first and
    # keeps each shot's parallel work stable between calls.
    assert [node.kind for node in plan[:1]] == [NodeKind.SCRIPT]
    assert [node.id for node in plan] == [node.id for node in repo.plan_rebuild(project)]


def test_versions_are_immutable_and_numbered(graph):
    repo, project, _ = graph
    node = repo.add_node(project, NodeKind.VOICEOVER, shot_index=99)
    node.recipe = {"text": "first"}
    first = repo.record_version(node, ["voice-1.wav"], cost=1.25, note="first")
    repo.session.commit()

    node.recipe = {"text": "second"}
    second = repo.record_version(node, ["voice-2.wav"], cost=2.5, note="second")
    repo.session.commit()

    assert first.number == 1
    assert second.number == 2
    assert first.recipe_snapshot == {"text": "first"}
    assert first.output_paths == ["voice-1.wav"]
    assert first.cost == 1.25
    assert second.recipe_snapshot == {"text": "second"}
    assert node.current_version_id == second.id
    assert node.status == NodeStatus.CLEAN
    assert [version.number for version in repo.list_versions(node)] == [1, 2]


def test_cache_hit_after_reverting_recipe_change(graph):
    repo, project, _ = graph
    node = repo.add_node(project, NodeKind.VOICEOVER, shot_index=99)
    node.recipe = {"text": "original"}
    original = repo.record_version(node, {"path": "original.wav"})
    repo.session.commit()

    repo.update_recipe(node, {"text": "changed"})
    repo.record_version(node, {"path": "changed.wav"})
    repo.session.commit()

    repo.revert(node, original.number)
    repo.session.commit()
    cached = repo.find_cached_version(node)
    assert cached is not None
    assert cached.id == original.id
    assert node.current_version_id == original.id
    assert node.recipe == {"text": "original"}


def test_revert_marks_dependents_dirty(graph):
    repo, project, nodes = graph
    node = nodes["voiceover"][1]
    version = repo.record_version(node, {"path": "v1.wav"})
    repo.session.commit()

    repo.update_recipe(node, {"text": "new"})
    repo.record_version(node, {"path": "v2.wav"})
    for item in project.nodes:
        if item is not node:
            repo.record_version(item, {})
    repo.session.commit()

    repo.revert(node, version.number)
    repo.session.commit()
    assert node.status == NodeStatus.CLEAN
    assert {item for item in project.nodes if item.status == NodeStatus.DIRTY} == {
        nodes["lipsync"][1],
        nodes["subtitles"][1],
        nodes["final"],
    }


def test_cycle_detection(repo):
    project = repo.create_project("cycle")
    first = repo.add_node(project, NodeKind.SCRIPT)
    second = repo.add_node(project, NodeKind.FINAL, depends_on=[first])
    first.depends_on.append(second)
    repo.session.flush()

    with pytest.raises(ValueError, match="cycle"):
        repo.plan_rebuild(project)


def test_version_numbers_are_unique_per_node(repo):
    project = repo.create_project("unique")
    node = repo.add_node(project, NodeKind.SCRIPT)
    version = repo.record_version(node, {}, note="first")
    duplicate = Version(
        node_id=node.id,
        number=version.number,
        recipe_snapshot={},
        output_paths={},
        recipe_hash="0" * 64,
    )
    repo.session.add(duplicate)

    with pytest.raises(IntegrityError):
        repo.session.flush()
    repo.session.rollback()


def test_versions_are_immutable(repo):
    project = repo.create_project("immutable")
    node = repo.add_node(project, NodeKind.SCRIPT)
    version = repo.record_version(node, {}, note="original")
    repo.session.commit()

    version.note = "changed"
    with pytest.raises(ValueError, match="immutable"):
        repo.session.flush()
    repo.session.rollback()


