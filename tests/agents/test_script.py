from __future__ import annotations

import json
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from avd.agents.analysis import StructureTemplate
from avd.agents.script import ScriptAgent, Storyboard, StoryboardShot
from avd.config import ModelPrice, Settings
from avd.llm import ChatResponse, CostLedger, FakeProvider, LLMRouter
from avd.state.consent import ConsentRequiredError
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def storyboard(count: int = 3) -> Storyboard:
    return Storyboard(
        title="A fresh explanation",
        shots=[
            StoryboardShot(
                index=index,
                duration_s=3.0,
                line=f"Original line {index}",
                visual_prompt=f"Presenter holding example {index}",
                camera="slow push in",
                emotion="curious",
            )
            for index in range(1, count + 1)
        ],
    )


def structure_template() -> StructureTemplate:
    return StructureTemplate(
        total_duration_s=9,
        hook="Start with a surprising question",
        shots=[
            {
                "duration_s": 3,
                "shot_size": "medium",
                "camera_move": "slow push in",
                "visual_description": "Presenter explains a practical example",
                "spoken_line_purpose": f"Explain step {index}",
            }
            for index in range(1, 4)
        ],
        pacing_notes="Three equally spaced beats",
        music_style="Soft ambient music",
        voice_style="Clear and warm",
    )


def make_agent(
    session: Session,
    tmp_path: Path,
    responses: list[ChatResponse | str | Exception] | None = None,
) -> tuple[ScriptAgent, Project, Character, Voice, FakeProvider]:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        script_model="test-script-model",
        qa_model="different-qa-model",
        model_prices={"test-script-model": ModelPrice(input_per_million=1)},
    )
    character = Character(
        name="My presenter",
        reference_images={
            "front": "assets/presenter/front.png",
            "side": "assets/presenter/side.png",
        },
        fixed_appearance_prompt="Short brown hair and a green jacket",
        consent_confirmed=True,
    )
    voice = Voice(
        name="My narration",
        sample_audio_paths=["assets/narration/sample.wav"],
        engine="test-tts",
        engine_voice_id="speaker-123",
        default_params={"speed": 1.1, "emotion": "neutral"},
        consent_confirmed=True,
    )
    session.add_all([character, voice])
    session.flush()
    repo = ProjectRepo(session)
    project = repo.create_project("Storyboard project", budget_limit=10)
    repo.attach_assets(project, character, voice)
    fake = FakeProvider(responses or [])
    router = LLMRouter(
        settings,
        providers={"openai": fake},
        ledger=CostLedger(session, settings.model_prices),
    )
    return ScriptAgent(repo, router, settings), project, character, voice, fake


def make_clean_graph(
    session: Session, tmp_path: Path
) -> tuple[ScriptAgent, Project, dict[str, Any]]:
    agent, project, _, _, _ = make_agent(session, tmp_path)
    graph = agent.apply_storyboard(project, storyboard())
    for node in project.nodes:
        if node.status != NodeStatus.CLEAN:
            agent.repo.record_version(node, {"path": f"generated/{node.id}.bin"})
    session.commit()
    return agent, project, graph


def snapshot(project: Project) -> dict[str, Any]:
    return {
        node.id: {
            "recipe": deepcopy(node.recipe),
            "status": node.status,
            "current_version_id": node.current_version_id,
            "versions": [
                (version.id, deepcopy(version.recipe_snapshot), deepcopy(version.output_paths))
                for version in node.versions
            ],
        }
        for node in project.nodes
    }


def test_write_storyboard_routes_structured_request_with_asset_context(
    session: Session, tmp_path: Path
) -> None:
    expected = storyboard()
    agent, project, character, voice, fake = make_agent(
        session, tmp_path, [ChatResponse(content=expected.model_dump_json(), input_tokens=25)]
    )
    result = agent.write_storyboard(
        structure_template(), character, voice, "Explain how to grow herbs indoors", project=project
    )
    assert result == expected
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "test-script-model"
    messages = json.dumps(fake.calls[0]["messages"], ensure_ascii=False)
    assert "Explain how to grow herbs indoors" in messages
    assert "Start with a surprising question" in messages
    assert "Short brown hair and a green jacket" in messages
    assert "My narration" in messages
    assert "duration_s" in messages and "visual_prompt" in messages
    assert agent.router.ledger is not None
    entries = agent.router.ledger.list(project.id)
    assert len(entries) == 1 and entries[0].role == "script"
    assert project.spent == pytest.approx(0.000025)


def test_write_storyboard_repairs_invalid_structured_output(
    session: Session, tmp_path: Path
) -> None:
    expected = storyboard()
    agent, project, character, voice, fake = make_agent(
        session, tmp_path, ['{"title": "Broken", "shots": []}', expected.model_dump_json()]
    )
    assert (
        agent.write_storyboard(
            structure_template(), character, voice, "A new lesson", project=project
        )
        == expected
    )
    assert len(fake.calls) == 2


@pytest.mark.parametrize("asset_name", ["character", "voice"])
def test_unconsented_assets_are_refused_before_model_call(
    session: Session, tmp_path: Path, asset_name: str
) -> None:
    agent, project, character, voice, fake = make_agent(session, tmp_path)
    asset = character if asset_name == "character" else voice
    asset.consent_confirmed = False
    session.flush()
    with pytest.raises(ConsentRequiredError):
        agent.write_storyboard(
            structure_template(), character, voice, "A new lesson", project=project
        )
    assert fake.calls == []


def test_apply_storyboard_creates_recipes_and_persists_a_clean_script(
    session: Session, tmp_path: Path
) -> None:
    agent, project, character, voice, _ = make_agent(session, tmp_path)
    expected = storyboard()
    graph = agent.apply_storyboard(project, expected)
    assert len(project.nodes) == 17
    assert graph["script"].kind == NodeKind.SCRIPT
    assert graph["script"].status == NodeStatus.CLEAN
    assert graph["script"].recipe == expected.model_dump(mode="json")
    versions = agent.repo.list_versions(graph["script"])
    assert len(versions) == 1
    assert versions[0].recipe_snapshot == expected.model_dump(mode="json")
    for offset, shot in enumerate(expected.shots):
        frame = graph["shot_frame"][offset]
        video = graph["shot_video"][offset]
        audio = graph["voiceover"][offset]
        assert frame.recipe["visual_prompt"] == shot.visual_prompt
        assert frame.recipe["character_id"] == character.id
        assert frame.recipe["reference_images"] == character.reference_images
        assert audio.recipe["line"] == shot.line
        assert audio.recipe["emotion"] == shot.emotion
        assert audio.recipe["voice_id"] == voice.id
        assert audio.recipe["default_params"] == voice.default_params
        assert video.recipe["camera"] == shot.camera
        assert video.recipe["duration_s"] == shot.duration_s
        assert frame.depends_on == [graph["script"]]
        assert video.depends_on == [frame]
        assert audio.depends_on == [graph["script"]]
        assert set(graph["lipsync"][offset].depends_on) == {video, audio}
    assert set(graph["final"].depends_on) == set(graph["subtitles"])
    assert len(agent.repo.plan_rebuild(project)) == 16
    session.commit()
    session.expire_all()
    assert graph["script"].current_version is not None
    assert graph["script"].current_version.recipe_snapshot == expected.model_dump(mode="json")


@pytest.mark.parametrize(
    ("changes", "dirty_kinds"),
    [
        ({"line": "A revised second line"}, {"voiceover", "lipsync", "subtitles", "final"}),
        ({"emotion": "excited"}, {"voiceover", "lipsync", "subtitles", "final"}),
        (
            {"visual_prompt": "A bright garden"},
            {"shot_frame", "shot_video", "lipsync", "subtitles", "final"},
        ),
        ({"camera": "gentle pan"}, {"shot_video", "lipsync", "subtitles", "final"}),
        ({"duration_s": 4.5}, {"shot_video", "lipsync", "subtitles", "final"}),
    ],
)
def test_shot_edit_dirties_only_the_required_chain(
    session: Session, tmp_path: Path, changes: dict[str, Any], dirty_kinds: set[str]
) -> None:
    agent, project, graph = make_clean_graph(session, tmp_path)
    before = snapshot(project)
    old_script = deepcopy(graph["script"].current_version.recipe_snapshot)
    agent.update_shot(project, 2, changes)
    dirty = {(node.kind.value, node.shot_index) for node in agent.repo.plan_rebuild(project)}
    assert dirty == {(kind, None if kind == "final" else 2) for kind in dirty_kinds}
    for node in project.nodes:
        if node.shot_index in {1, 3}:
            assert snapshot(project)[node.id] == before[node.id]
    assert graph["script"].status == NodeStatus.CLEAN
    assert graph["script"].current_version.recipe_snapshot["shots"][1] == {
        **old_script["shots"][1],
        **changes,
    }
    assert agent.repo.list_versions(graph["script"])[0].recipe_snapshot == old_script
    session.commit()
    session.expire_all()
    for key, value in changes.items():
        assert graph["script"].recipe["shots"][1][key] == value


def test_multiple_shot_fields_update_their_respective_recipes(
    session: Session, tmp_path: Path
) -> None:
    agent, project, graph = make_clean_graph(session, tmp_path)
    agent.update_shot(project, 2, {"line": "New words", "camera": "locked off", "emotion": "warm"})
    assert graph["voiceover"][1].recipe["line"] == "New words"
    assert graph["voiceover"][1].recipe["emotion"] == "warm"
    assert graph["shot_video"][1].recipe["camera"] == "locked off"
    assert graph["shot_frame"][1].status == NodeStatus.CLEAN
    assert {node.kind for node in agent.repo.plan_rebuild(project)} == {
        NodeKind.VOICEOVER,
        NodeKind.SHOT_VIDEO,
        NodeKind.LIPSYNC,
        NodeKind.SUBTITLES,
        NodeKind.FINAL,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": "value"},
        {"index": 1},
        {"line": "Must not be partially applied", "duration_s": 0},
        {"duration_s": -1},
        {"duration_s": float("inf")},
        {"duration_s": float("nan")},
    ],
)
def test_invalid_shot_edit_is_atomic(
    session: Session, tmp_path: Path, changes: dict[str, Any]
) -> None:
    agent, project, _ = make_clean_graph(session, tmp_path)
    before = snapshot(project)
    with pytest.raises(ValueError):
        agent.update_shot(project, 2, changes)
    assert snapshot(project) == before


@pytest.mark.parametrize("shot_index", [0, 4])
def test_missing_shot_edit_leaves_graph_unchanged(
    session: Session, tmp_path: Path, shot_index: int
) -> None:
    agent, project, _ = make_clean_graph(session, tmp_path)
    before = snapshot(project)
    with pytest.raises(ValueError):
        agent.update_shot(project, shot_index, {"line": "No such shot"})
    assert snapshot(project) == before


def test_reapplying_storyboard_preserves_graph_and_immutable_versions(
    session: Session, tmp_path: Path
) -> None:
    agent, project, graph = make_clean_graph(session, tmp_path)
    before = snapshot(project)
    revised = storyboard().model_copy(deep=True)
    revised.shots[1].line = "A newly approved second line"
    new_graph = agent.apply_storyboard(project, revised)
    assert set(snapshot(project)) == set(before)
    assert new_graph["script"].id == graph["script"].id
    assert new_graph["voiceover"][1].recipe["line"] == revised.shots[1].line
    for node in project.nodes:
        old_versions = before[node.id]["versions"]
        current_versions = snapshot(project)[node.id]["versions"]
        assert current_versions[: len(old_versions)] == old_versions
    assert {(node.kind.value, node.shot_index) for node in agent.repo.plan_rebuild(project)} == {
        ("voiceover", 2),
        ("lipsync", 2),
        ("subtitles", 2),
        ("final", None),
    }


def test_reapplying_with_a_different_shot_count_is_atomic(session: Session, tmp_path: Path) -> None:
    agent, project, _ = make_clean_graph(session, tmp_path)
    before = snapshot(project)
    with pytest.raises(ValueError):
        agent.apply_storyboard(project, storyboard(count=2))
    assert snapshot(project) == before


@pytest.mark.parametrize("indexes", [[0], [2], [1, 1], [1, 3]])
def test_storyboard_requires_unique_contiguous_one_based_indexes(indexes: list[int]) -> None:
    data = storyboard(len(indexes)).model_dump()
    for shot, index in zip(data["shots"], indexes, strict=True):
        shot["index"] = index
    with pytest.raises(ValidationError):
        Storyboard.model_validate(data)


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_storyboard_rejects_nonpositive_and_nonfinite_durations(duration: float) -> None:
    data = storyboard(1).model_dump()
    data["shots"][0]["duration_s"] = duration
    with pytest.raises(ValidationError):
        Storyboard.model_validate(data)


def test_storyboard_requires_at_least_one_shot() -> None:
    with pytest.raises(ValidationError):
        Storyboard(title="Empty", shots=[])
