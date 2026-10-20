from pathlib import Path

import pytest
from sqlalchemy import select

from avd.agents.analysis import StructureTemplate
from avd.agents.script import Storyboard, StoryboardShot
from avd.config import Settings
from avd.director.review import ReviewRequired, approve
from avd.jobs import Job, JobStatus, Worker
from avd.llm import FakeProvider
from avd.runtime import Runtime
from avd.state.consent import ConsentRequiredError
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


def test_real_authoring_registry_runs_script_job_and_rechecks_generation(tmp_path: Path) -> None:
    board = Storyboard(
        title="Original",
        shots=[
            StoryboardShot(
                index=1,
                duration_s=3,
                line="An original line",
                visual_prompt="A garden",
                camera="static",
                emotion="calm",
            )
        ],
    )
    runtime = Runtime(
        Settings(_env_file=None, data_dir=tmp_path),
        providers={"openai": FakeProvider([board.model_dump_json()])},
    )
    with runtime.session_factory() as session:
        repo = ProjectRepo(session)
        character = Character(
            name="C", consent_confirmed=True, reference_images={"front": "uploads/ref.png"}
        )
        voice = Voice(name="V", consent_confirmed=True, sample_audio_paths=["uploads/ref.wav"])
        session.add_all([character, voice])
        session.flush()
        project = repo.create_project("P", budget_limit=10)
        repo.attach_assets(project, character, voice)
        project.analysis_result = StructureTemplate(
            total_duration_s=3,
            hook="Open a question",
            pacing_notes="calm",
            music_style="none",
            voice_style="clear",
            shots=[
                dict(
                    duration_s=3,
                    shot_size="wide",
                    camera_move="static",
                    visual_description="Nature",
                    spoken_line_purpose="Introduce",
                )
            ],
        ).model_dump(mode="json")
        analysis = repo.add_node(project, NodeKind.ANALYSIS)
        repo.record_version(analysis, {})
        approve(session, project, "structure", "human")
        project_id = project.id
        session.commit()
    job_id = runtime.queue.submit(
        "api", "write_script", {"brief": "An original garden video"}, project_id=project_id
    )
    Worker(runtime.queue, "api", runtime.build_registry()).run_once()
    assert runtime.queue.get(job_id).status == JobStatus.SUCCEEDED
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        script = session.scalar(select(Node).where(Node.kind == NodeKind.SCRIPT))
        frame = session.scalar(select(Node).where(Node.kind == NodeKind.SHOT_FRAME))
        assert (
            script.status == NodeStatus.CLEAN
            and script.recipe["shots"][0]["line"] == "An original line"
        )
        frame_id = frame.id
    job = Job(
        project_id=project_id, node_id=frame_id, job_type="image_gen", payload={"estimated_cost": 0}
    )
    with pytest.raises(ReviewRequired):
        runtime.check_job(job)
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        approve(session, project, "script", "human")
        session.commit()
    runtime.check_job(job)
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        project.character.consent_confirmed = False
        session.commit()
    with pytest.raises(ConsentRequiredError):
        runtime.check_job(job)
    runtime.close()


def test_asset_replacement_updates_materialized_recipe_and_dirty_chain(tmp_path: Path) -> None:
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    with runtime.session_factory() as session:
        repo = ProjectRepo(session)
        old = Character(name="Old", consent_confirmed=True)
        new = Character(
            name="New", consent_confirmed=True, reference_images={"front": "assets/new.png"}
        )
        voice = Voice(name="V", consent_confirmed=True)
        session.add_all([old, new, voice])
        session.flush()
        project = repo.create_project("P")
        repo.attach_assets(project, old, voice)
        graph = repo.create_shot_graph(project, 1)
        for node in project.nodes:
            repo.record_version(node, {})
        repo.attach_assets(project, new, voice)
        assert graph["shot_frame"][0].recipe["character_id"] == new.id
        assert graph["shot_frame"][0].recipe["reference_images"] == new.reference_images
        assert graph["voiceover"][0].status == NodeStatus.CLEAN
        assert graph["lipsync"][0].status == NodeStatus.DIRTY
        assert graph["final"].status == NodeStatus.DIRTY
    runtime.close()
