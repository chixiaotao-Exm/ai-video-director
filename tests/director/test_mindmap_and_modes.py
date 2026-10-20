from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy.orm import Session

from avd.agents.analysis.models import StoryBible, StructureTemplate
from avd.director import mindmap
from avd.director.mindmap import MindMapItem
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def _template(**extra: Any) -> dict[str, Any]:
    return StructureTemplate(
        total_duration_s=10,
        summary="一段弹唱",
        hook="直接开唱",
        shots=[
            {
                "duration_s": 10,
                "shot_size": "中景",
                "camera_move": "固定",
                "visual_description": "人物弹琴",
                "spoken_line_purpose": "抒情",
            }
        ],
        pacing_notes="一镜到底",
        music_style="钢琴",
        voice_style="抒情",
        **extra,
    ).model_dump(mode="json")


def test_derived_branches_and_director_nodes(session: Session) -> None:
    project = ProjectRepo(session).create_project("弹唱", source_video_path="uploads/a.mp4")
    project.analysis_result = _template()
    mindmap.update(
        session,
        project,
        [
            MindMapItem(id="dir-a", label="深夜弹唱", kind="option"),
            MindMapItem(id="pick", parent="dir-a", label="选 A", kind="decision", status="chosen"),
            MindMapItem(id="orphan", parent="missing", label="孤儿"),
        ],
        [],
    )
    nodes = {node["id"]: node for node in mindmap.tree(session, project)["nodes"]}
    assert nodes["root"]["label"] == "弹唱"
    assert nodes["reference"]["note"] == "一段弹唱"
    assert nodes["reference.hook"]["parent"] == "reference"
    assert nodes["reference.shot.1"]["parent"] == "reference"
    assert nodes["pick"]["parent"] == "dir-a"
    assert nodes["orphan"]["parent"] == "root"


def test_update_upserts_and_removes_descendants(session: Session) -> None:
    project = ProjectRepo(session).create_project("p")
    mindmap.update(
        session,
        project,
        [
            MindMapItem(id="a", label="A"),
            MindMapItem(id="b", parent="a", label="B"),
            MindMapItem(id="c", parent="b", label="C"),
        ],
        [],
    )
    mindmap.update(session, project, [MindMapItem(id="a", label="A2", status="chosen")], [])
    ids = {node["id"]: node for node in mindmap.tree(session, project)["nodes"]}
    assert ids["a"]["label"] == "A2" and ids["a"]["status"] == "chosen"
    result = mindmap.update(session, project, [], ["b"])
    assert result["removed"] == ["b", "c"]
    assert {node["id"] for node in mindmap.tree(session, project)["nodes"]} == {"root", "a"}


def test_reserved_ids_are_rejected(session: Session) -> None:
    project = ProjectRepo(session).create_project("p")
    for reserved in ("root", "reference", "shot-1"):
        with pytest.raises(ValueError, match="reserved"):
            mindmap.update(session, project, [MindMapItem(id=reserved, label="x")], [])


def test_projects_default_to_remake_and_accept_anime_mode(session: Session) -> None:
    project = ProjectRepo(session).create_project("p")
    session.flush()
    assert project.mode == "remake"
    anime = Project(title="续集", mode="anime_sequel")
    session.add(anime)
    session.flush()
    assert session.get(Project, anime.id).mode == "anime_sequel"


def test_story_bible_is_optional_and_validated() -> None:
    assert StructureTemplate.model_validate(_template()).story_bible is None
    bible = StoryBible(characters=[{"name": "小葵", "role": "主角"}], cliffhanger="门开了")
    template = StructureTemplate.model_validate(_template(story_bible=bible))
    assert template.story_bible is not None
    assert template.story_bible.characters[0].name == "小葵"
