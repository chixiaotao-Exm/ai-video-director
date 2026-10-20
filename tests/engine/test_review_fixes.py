"""Durable accounting and immutable execution snapshots from the T11 audit."""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from avd.config import ComfyUIProfile, Settings
from avd.engine import RebuildEngine
from avd.executors import build_registry, handlers
from avd.jobs import Job, JobQueue, JobStatus
from avd.llm import BudgetExceeded, CostEntry
from avd.state.consent import ConsentRequiredError
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Character, Node, NodeKind, NodeStatus, Project, Voice
from avd.state.repo import ProjectRepo


@dataclass
class Harness:
    sessions: sessionmaker[Session]
    settings: Settings
    queue: JobQueue
    engine: RebuildEngine
    project_id: str

    def add(self, kind: NodeKind, *, parent: str | None = None) -> str:
        with self.sessions() as session:
            project = session.get(Project, self.project_id)
            assert project is not None
            dependency = session.get(Node, parent) if parent else None
            node = ProjectRepo(session).add_node(
                project,
                kind,
                recipe={"mode": "character_sheet", "appearance_prompt": "robot", "line": "hello"},
                depends_on=[dependency] if dependency else [],
            )
            session.commit()
            return node.id

    def claim(self, lane: str = "api", *, paid: bool = False) -> Job:
        job = self.queue.claim_next(lane)
        assert job is not None
        if paid:
            with self.sessions() as session:
                saved = session.get(Job, job.id)
                assert saved is not None
                payload = dict(saved.payload)
                payload["_started_paid_attempts"] = [
                    *payload.get("_started_paid_attempts", []),
                    job.attempts,
                ]
                saved.payload = payload
                session.commit()
                job.payload = payload
        return job

    def complete(self, job: Job, *, cost: float = 0.25) -> Job:
        output = self.settings.data_dir / job.payload["output_dir"] / f"{job.attempts}.bin"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"generated")
        return self.queue.complete(
            job.id,
            {
                "outputs": {"image": output.relative_to(self.settings.data_dir).as_posix()},
                "cost": cost,
            },
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    settings = Settings(
        data_dir=tmp_path,
        confirm_threshold=10,
        job_estimated_costs={"image_gen": 0.25, "tts": 0.25},
        _env_file=None,
    )
    database = create_engine(settings.database_url)
    init_db(database)
    sessions = create_session_factory(database)
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"voice")
    with sessions() as session:
        project = Project(
            title="Review fixes",
            budget_limit=10,
            character=Character(name="Robot", consent_confirmed=True),
            voice=Voice(name="Voice", consent_confirmed=True, sample_audio_paths=[sample.name]),
        )
        session.add(project)
        session.commit()
        project_id = project.id
    queue = JobQueue(sessions)
    yield Harness(sessions, settings, queue, RebuildEngine(sessions, queue, settings), project_id)
    database.dispose()


@pytest.mark.parametrize("blocked_by", ["budget", "consent"])
def test_reconcile_commits_before_next_scheduling_error(harness: Harness, blocked_by: str) -> None:
    frame_id = harness.add(NodeKind.SHOT_FRAME)
    harness.add(NodeKind.VOICEOVER, parent=frame_id)
    harness.engine.rebuild(harness.project_id)
    completed = harness.complete(harness.claim())
    with harness.sessions() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None and project.voice is not None
        if blocked_by == "budget":
            project.budget_limit = 0.4
        else:
            project.voice.consent_confirmed = False
        session.commit()
    error = BudgetExceeded if blocked_by == "budget" else ConsentRequiredError
    with pytest.raises(error):
        harness.engine.rebuild(harness.project_id)
    with harness.sessions() as session:
        node = session.get(Node, frame_id)
        saved = session.get(Job, completed.id)
        assert node is not None and saved is not None
        assert node.status == NodeStatus.CLEAN
        assert len(node.versions) == 1
        assert node.project.spent == pytest.approx(0.25)
        assert saved.result is not None and saved.result["_engine_handled"]


def test_cancelled_job_releases_node_and_allows_rebuild(harness: Harness) -> None:
    node_id = harness.add(NodeKind.SHOT_FRAME)
    (old_id,) = harness.engine.rebuild(harness.project_id)
    assert harness.queue.cancel(old_id)
    harness.engine.rebuild(harness.project_id, kinds=[])
    with harness.sessions() as session:
        node = session.get(Node, node_id)
        assert node is not None and node.status == NodeStatus.DIRTY
        assert node.project.spent == 0
    (new_id,) = harness.engine.rebuild(harness.project_id)
    assert new_id != old_id


def test_explicit_retry_uses_corrected_recipe_and_a_new_job(harness: Harness) -> None:
    node_id = harness.add(NodeKind.SHOT_FRAME)
    (old_id,) = harness.engine.rebuild(harness.project_id)
    claimed = harness.claim()
    harness.engine.on_finished(harness.queue.fail(claimed.id, "bad prompt", retry=False))
    assert harness.engine.rebuild(harness.project_id) == []
    with harness.sessions() as session:
        node = session.get(Node, node_id)
        assert node is not None and node.status == NodeStatus.FAILED
        node.recipe = {**node.recipe, "appearance_prompt": "corrected robot"}
        session.commit()
    (new_id,) = harness.engine.retry_failed(harness.project_id)
    replacement = harness.queue.get(new_id)
    assert new_id != old_id and replacement is not None
    assert replacement.payload["recipe"]["appearance_prompt"] == "corrected robot"


def test_each_paid_failed_attempt_and_success_is_charged_once(harness: Harness) -> None:
    harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    first = harness.claim(paid=True)
    retrying = harness.queue.fail(first.id, "provider output could not be decoded")
    assert retrying.status == JobStatus.QUEUED
    harness.engine.on_finished(retrying)
    with harness.sessions() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None and project.spent == pytest.approx(0.25)
    second = harness.claim(paid=True)
    completed = harness.complete(second, cost=0.3)
    harness.engine.on_finished(completed)
    harness.engine.on_finished(completed)
    harness.engine.rebuild(harness.project_id)
    with harness.sessions() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None and project.spent == pytest.approx(0.55)
        entries = list(session.scalars(select(CostEntry).order_by(CostEntry.attempt)))
        assert [(entry.job_id, entry.attempt, entry.est_cost) for entry in entries] == [
            (first.id, 1, 0.25),
            (first.id, 2, 0.3),
        ]


@pytest.mark.parametrize("started", [False, True])
def test_failed_attempt_is_charged_only_after_executor_started(
    harness: Harness, started: bool
) -> None:
    harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim(paid=started)
    failed = harness.queue.fail(job.id, "guard or provider failed", retry=False)
    harness.engine.on_finished(failed)
    harness.engine.on_finished(failed)
    with harness.sessions() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None and project.spent == pytest.approx(0.25 if started else 0)
        assert len(list(session.scalars(select(CostEntry)))) == int(started)


def test_invalid_output_keeps_estimated_paid_attempt_cost(harness: Harness) -> None:
    node_id = harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim(paid=True)
    completed = harness.queue.complete(job.id, {"outputs": {"image": "missing.png"}, "cost": 0.3})
    harness.engine.on_finished(completed)
    with harness.sessions() as session:
        node = session.get(Node, node_id)
        assert node is not None and node.status == NodeStatus.FAILED
        assert not node.versions
        assert node.project.spent == pytest.approx(0.25)


@pytest.mark.parametrize("outputs", [None, {}, {"image": None}, {"image": 4}, {"image": "empty"}])
def test_cache_rejects_malformed_and_empty_outputs(harness: Harness, outputs: Any) -> None:
    (harness.settings.data_dir / "empty").touch()
    assert not harness.engine._outputs_exist(outputs)


def test_image_model_change_invalidates_cache(harness: Harness) -> None:
    node_id = harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim()
    harness.engine.on_finished(harness.complete(job))
    harness.settings.image_model = "new-image-model"
    with harness.sessions() as session:
        node = session.get(Node, node_id)
        assert node is not None
        ProjectRepo(session).update_recipe(node, {})
        session.commit()
    (new_id,) = harness.engine.rebuild(harness.project_id)
    new_job = harness.queue.get(new_id)
    assert new_job is not None
    assert new_job.payload["recipe_hash"] != job.payload["recipe_hash"]
    assert new_job.payload["recipe"]["_execution"]["image_model"] == "new-image-model"


def test_queued_image_executes_captured_model(harness: Harness) -> None:
    captured: list[str] = []

    class Image:
        def generate(self, *, model: str, output_path: Path, **_: Any) -> Path:
            captured.append(model)
            output_path.write_bytes(b"image")
            return output_path

    harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim()
    expected = harness.settings.image_model
    harness.settings.image_model = "different-worker-model"
    handler = build_registry(harness.settings, image_backend=Image()).get("image_gen")
    assert handler is not None
    completed = harness.queue.complete(job.id, handler(job))
    harness.engine.on_finished(completed)
    assert captured == [expected]
    with harness.sessions() as session:
        node = session.get(Node, job.node_id)
        assert node is not None and node.status == NodeStatus.CLEAN
        assert node.current_version is not None
        assert node.current_version.recipe_snapshot["_execution"]["image_model"] == expected


def test_paid_callback_runs_at_provider_boundary_after_local_validation(harness: Harness) -> None:
    events: list[str] = []

    class Image:
        def generate(self, **_: Any) -> Path:
            events.append("provider")
            raise RuntimeError("provider failed after accepting request")

    harness.add(NodeKind.SHOT_FRAME)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim()
    handler = build_registry(
        harness.settings,
        image_backend=Image(),
        before_paid=lambda _: events.append("paid"),
    ).get("image_gen")
    assert handler is not None
    original_recipe = job.payload["recipe"]
    job.payload = {**job.payload, "recipe": {}}
    with pytest.raises(ValueError, match="reference images"):
        handler(job)
    assert events == []
    job.payload = {**job.payload, "recipe": original_recipe}
    with pytest.raises(RuntimeError, match="provider failed"):
        handler(job)
    assert events == ["paid", "provider"]


def test_workflow_content_changes_hash_and_queued_job_keeps_snapshot(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = harness.settings.data_dir / "workflows"
    directory.mkdir()
    template = {
        "1": {
            "class_type": "OriginalTTS",
            "inputs": {
                "text": "{{text}}",
                "voice_sample": "{{voice_sample}}",
                "speed": "{{speed}}",
                "emotion": "{{emotion}}",
            },
        },
    }
    template_path = directory / "tts.json"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    harness.settings.workflow_dir = directory
    harness.settings.comfyui_profiles["local"] = ComfyUIProfile(timeout=42)
    node_id = harness.add(NodeKind.VOICEOVER)
    harness.engine.rebuild(harness.project_id)
    job = harness.claim("gpu")
    template["1"]["class_type"] = "ReplacementTTS"
    template_path.write_text(json.dumps(template), encoding="utf-8")
    with harness.sessions() as session:
        node = session.get(Node, node_id)
        assert node is not None
        assert (
            harness.engine._effective_recipe(node)["_execution"]
            != job.payload["recipe"]["_execution"]
        )
    harness.settings.comfyui_profiles["local"] = ComfyUIProfile(timeout=99)
    submitted: list[dict[str, Any]] = []
    timeouts: list[float | None] = []

    class Comfy:
        def upload_file(self, path: str | Path) -> str:
            return "voice.wav"

        def submit(self, workflow: Any) -> str:
            submitted.append(workflow)
            return "prompt"

        def wait(
            self, prompt_id: str, poll_interval: float = 0.25, timeout: float | None = None
        ) -> dict[str, Any]:
            timeouts.append(timeout)
            return {}

        def download_outputs(self, prompt_id: str, dest_dir: str | Path) -> list[Path]:
            output = Path(dest_dir) / "voice.wav"
            output.write_bytes(b"voice output")
            return [output]

    monkeypatch.setattr(handlers, "media_duration", lambda _: 1.0)
    handler = build_registry(harness.settings, comfyui_client=Comfy()).get("tts")
    assert handler is not None
    handler(job)
    assert submitted[0]["1"]["class_type"] == "OriginalTTS"
    assert timeouts == [42]
