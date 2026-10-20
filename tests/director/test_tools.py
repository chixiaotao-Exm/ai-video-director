from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from avd.agents.script import Storyboard, StoryboardShot, apply_storyboard
from avd.config import Settings
from avd.director.review import (
    ReviewRequired,
    approve,
    approve_budget,
    budget_is_confirmed,
    require_review,
    review_status,
)
from avd.director.tools import DirectorTools
from avd.engine.rebuild import RebuildEngine
from avd.jobs import JobQueue
from avd.llm import ConfirmationRequired
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, Node, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@pytest.fixture
def harness(tmp_path: Path) -> Any:
    settings = Settings(data_dir=tmp_path, gemini_model="fake-analysis", _env_file=None)
    engine = create_engine(settings.database_url)
    init_db(engine)
    factory = create_session_factory(engine)
    queue = JobQueue(factory)
    runtime = SimpleNamespace(settings=settings, session_factory=factory, queue=queue)
    runtime.engine = RebuildEngine(factory, queue, settings)
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "source.mp4").write_bytes(b"fake video")
    with factory() as session:
        repo = ProjectRepo(session)
        project = repo.create_project(
            "Example", source_video_path="uploads/source.mp4", budget_limit=20
        )
        character = Character(
            name="Consented presenter",
            reference_images={"front": "assets/front.png"},
            consent_confirmed=True,
        )
        voice = Voice(
            name="Consented voice", sample_audio_paths=["assets/voice.wav"], consent_confirmed=True
        )
        session.add_all([character, voice])
        session.flush()
        repo.attach_assets(project, character, voice)
        analysis = repo.add_node(project, "analysis", recipe={"source": "source.mp4"})
        repo.record_version(analysis, {})
        board = Storyboard(
            title="Original story",
            shots=[
                StoryboardShot(
                    index=index,
                    duration_s=2,
                    line=f"Original line {index}",
                    visual_prompt=f"Presenter in scene {index}",
                    camera="static",
                    emotion="warm",
                )
                for index in (1, 2)
            ],
        )
        graph = apply_storyboard(project, board, repo=repo)
        runtime.project_id = project.id
        runtime.node_ids = {
            key: value.id for key, value in graph.items() if isinstance(value, Node)
        }
        runtime.frame_ids = [node.id for node in graph["shot_frame"]]
        session.commit()
    runtime.tools = DirectorTools(runtime, runtime.project_id)
    yield runtime
    engine.dispose()


def approve_stage(harness: Any, stage: str) -> None:
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        approve(session, project, stage, "human@example.test")
        session.commit()


def finish_frames(harness: Any) -> None:
    with harness.session_factory() as session:
        repo = ProjectRepo(session)
        for node_id in harness.frame_ids:
            node = session.get(Node, node_id)
            assert node is not None
            repo.record_version(node, {"image": "placeholder.png"})
        session.commit()


def test_all_tools_have_closed_input_schemas(harness: Any) -> None:
    schemas = harness.tools.schemas()
    assert len(schemas) == 24
    assert len({schema["name"] for schema in schemas}) == 24
    assert all(schema["input_schema"]["additionalProperties"] is False for schema in schemas)
    for schema in schemas:
        assert not {"confirmed", "consent_confirmed", "approved_by", "project_id"} & set(
            schema["input_schema"].get("properties", {})
        )
    with pytest.raises(ValueError, match="Unknown director tool"):
        harness.tools.execute("approve", {})
    with pytest.raises(ValidationError):
        harness.tools.execute("rebuild", {"confirmed": True})
    with pytest.raises(ValidationError):
        harness.tools.execute("update_shot", {"shot_index": 1, "changes": {"model": "other"}})
    with pytest.raises(ValidationError):
        harness.tools.execute("update_shot", {"shot_index": 1, "changes": {"line": None}})


def test_analysis_is_nonblocking_and_deduplicated(harness: Any) -> None:
    first = json.loads(harness.tools.execute("analyze_video", {}))
    second = json.loads(harness.tools.execute("analyze_video", {}))
    assert first["job_id"] == second["job_id"]
    job = harness.queue.get(first["job_id"])
    assert job is not None
    assert job.lane == "api"
    assert job.job_type == "analyze_video"
    assert job.payload["project_id"] == harness.project_id
    assert job.status == "queued"
    with pytest.raises(ReviewRequired, match="pending authoring"):
        approve_stage(harness, "structure")


def test_script_requires_human_structure_approval_and_queues(harness: Any) -> None:
    with pytest.raises(ReviewRequired):
        harness.tools.execute("write_script", {"brief": "Teach one idea"})
    approve_stage(harness, "structure")
    result = json.loads(harness.tools.execute("write_script", {"brief": "Teach one idea"}))
    job = harness.queue.get(result["job_id"])
    assert job is not None
    assert job.job_type == "write_script" and job.lane == "api"
    assert job.payload["brief"] == "Teach one idea"
    assert job.payload["confirmed"] is False


@pytest.mark.parametrize(
    "tool",
    [
        "generate_frames",
        "generate_videos",
        "generate_voiceover",
        "run_lipsync",
        "build_subtitles",
        "render_final",
        "rebuild",
    ],
)
def test_generations_need_human_script_approval(harness: Any, tool: str) -> None:
    with pytest.raises(ReviewRequired):
        harness.tools.execute(tool, {})
    assert harness.queue.list(harness.project_id) == []


def test_full_rebuild_stops_at_frames_and_cannot_self_approve(harness: Any) -> None:
    approve_stage(harness, "script")
    result = json.loads(harness.tools.execute("rebuild", {}))
    assert len(result["job_ids"]) == 2
    assert "human frame approval" in result["message"]
    jobs = harness.queue.list(harness.project_id)
    assert {job.job_type for job in jobs} == {"image_gen"}
    assert all(job.payload["selected_kinds"] == ["shot_frame"] for job in jobs)
    with pytest.raises(ReviewRequired):
        harness.tools.execute("generate_videos", {})


def test_frames_approval_allows_only_requested_generation(harness: Any) -> None:
    approve_stage(harness, "script")
    finish_frames(harness)
    approve_stage(harness, "frames")
    result = json.loads(harness.tools.execute("generate_videos", {}))
    assert len(result["job_ids"]) == 2
    assert {job.job_type for job in harness.queue.list(harness.project_id)} == {"image_to_video"}


def test_version_bound_review_is_invalidated_by_edit_and_dirty_output(harness: Any) -> None:
    approve_stage(harness, "script")
    finish_frames(harness)
    approve_stage(harness, "frames")
    harness.tools.execute("update_shot", {"shot_index": 1, "changes": {"line": "Revised line"}})
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        with pytest.raises(ReviewRequired):
            require_review(session, project, "script")
        assert review_status(session, project)["frames"]["approved"] is False
        frame = session.get(Node, harness.frame_ids[0])
        assert frame is not None
        frame.status = NodeStatus.DIRTY
        with pytest.raises(ReviewRequired, match="not ready"):
            approve(session, project, "frames", "human")


def test_batch_budget_requires_persisted_human_approval(harness: Any) -> None:
    approve_stage(harness, "script")
    harness.settings.job_estimated_costs = {"image_gen": 0.6}
    with pytest.raises(ConfirmationRequired):
        harness.tools.execute("generate_frames", {})
    assert harness.queue.list(harness.project_id) == []
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        approve_budget(session, project, 1.2, "human")
        assert budget_is_confirmed(session, project, 1.2)
        assert not budget_is_confirmed(session, project, 1.3)
        session.commit()
    result = json.loads(harness.tools.execute("generate_frames", {}))
    assert len(result["job_ids"]) == 2
    assert all(job.payload["confirmed"] for job in harness.queue.list(harness.project_id))


def test_budget_approval_does_not_survive_recipe_edit_or_unbounded_spend(harness: Any) -> None:
    with harness.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None
        approve_budget(session, project, 2, "human")
        project.spent = 1.5
        assert budget_is_confirmed(session, project, 0.5)
        assert not budget_is_confirmed(session, project, 0.6)
        frame = session.get(Node, harness.frame_ids[0])
        assert frame is not None
        ProjectRepo(session).update_recipe(frame, {"visual_prompt": "Changed image"})
        assert not budget_is_confirmed(session, project, 0)


def test_uploaded_assets_start_without_consent(harness: Any) -> None:
    (harness.settings.data_dir / "uploads" / "front.png").write_bytes(b"fake image")
    (harness.settings.data_dir / "uploads" / "sample.wav").write_bytes(b"fake audio")
    character = json.loads(
        harness.tools.execute(
            "create_character",
            {
                "name": "Original presenter",
                "reference_images": {"front": "uploads/front.png"},
                "appearance_prompt": "Blue shirt",
            },
        )
    )
    voice = json.loads(
        harness.tools.execute(
            "create_voice",
            {
                "name": "Own voice",
                "sample_paths": ["uploads/sample.wav"],
                "engine": "fake",
            },
        )
    )
    with harness.session_factory() as session:
        saved_character = session.get(Character, character["character_id"])
        saved_voice = session.get(Voice, voice["voice_id"])
        assert saved_character is not None and saved_character.consent_confirmed is False
        assert saved_voice is not None and saved_voice.consent_confirmed is False
        assert not Path(saved_character.reference_images["front"]).is_absolute()


@pytest.mark.parametrize(
    "path", ["../secret.png", "C:/outside/front.png", "/tmp/front.png", "assets/front.png"]
)
def test_character_cannot_read_outside_uploads(harness: Any, path: str) -> None:
    with pytest.raises(ValueError, match="relative|uploads"):
        harness.tools.execute(
            "create_character",
            {
                "name": "Presenter",
                "reference_images": {"front": path},
                "appearance_prompt": "Blue shirt",
            },
        )


def test_versions_and_revert_cannot_cross_projects(harness: Any) -> None:
    with harness.session_factory() as session:
        repo = ProjectRepo(session)
        foreign = repo.create_project("Other project")
        node = repo.add_node(foreign, "script")
        repo.record_version(node, {})
        node_id = node.id
        session.commit()
    for name, args in (
        ("list_versions", {"node_id": node_id}),
        ("revert", {"node_id": node_id, "version_number": 1}),
    ):
        with pytest.raises(KeyError, match="this project"):
            harness.tools.execute(name, args)


def test_read_tools_return_scoped_status_budget_and_version_history(harness: Any) -> None:
    status = json.loads(harness.tools.execute("get_project_status", {}))
    assert status["project_id"] == harness.project_id
    assert status["reviews"]["script"]["approved"] is False
    assert len(json.loads(harness.tools.execute("plan_rebuild", {}))) == 11
    budget = json.loads(harness.tools.execute("get_budget", {}))
    assert budget["remaining"] == 20
    versions = json.loads(
        harness.tools.execute("list_versions", {"node_id": harness.node_ids["script"]})
    )
    assert [version["number"] for version in versions] == [1]
