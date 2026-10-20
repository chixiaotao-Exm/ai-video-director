from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from avd.assets.library import AssetLibrary, ConsentRequiredError
from avd.assets.sheets import make_sheet_handler, submit_character_sheet
from avd.assets.storage import AssetStorage
from avd.config import Settings
from avd.director.review import approve_budget
from avd.jobs import Job, JobLane, JobStatus
from avd.llm import BudgetExceeded, ConfirmationRequired
from avd.runtime import Runtime
from avd.state.models import Character, Project
from avd.state.repo import ProjectRepo

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    instance = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            job_estimated_costs={"image_gen": 0.25},
            confirm_threshold=1,
            image_model="sheet-test-model",
        )
    )
    yield instance
    instance.close()


def project_id(runtime: Runtime, budget: float = 10) -> str:
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Character review", budget_limit=budget)
        session.commit()
        return project.id


class FakeImageHandler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, job: Job) -> dict[str, Any]:
        self.calls.append(job.id)
        root = Path(job.payload["data_dir"])
        path = root / job.payload["output_dir"] / f"attempt-{job.attempts}" / "output.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(PNG)
        return {
            "outputs": {"image": path.relative_to(root).as_posix()},
            "cost": job.payload["estimated_cost"],
        }


def test_submission_queues_prompt_only_sheet_and_snapshots_image_model(runtime: Runtime) -> None:
    pid = project_id(runtime)
    job_id = submit_character_sheet(runtime, pid, "Ada", "An original illustrated botanist")
    job = runtime.queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.lane == JobLane.API
    assert job.job_type == "character_sheet"
    assert job.node_id is None and job.project_id == pid
    assert job.model_key == "sheet-test-model"
    assert job.payload["recipe"] == {
        "character_sheet": True,
        "appearance_prompt": "An original illustrated botanist",
        "_execution": {"image_model": "sheet-test-model"},
    }
    assert job.payload["name"] == "Ada"
    assert job.payload["context"] == job.payload["inputs"] == {}
    assert job.payload["estimated_cost"] == 0.25
    assert job.payload["output_dir"].startswith(f"projects/{pid}/character-sheet-")
    assert "/1-" in job.payload["output_dir"]
    with runtime.session_factory() as session:
        assert list(session.scalars(select(Character))) == []
        assert session.get(Project, pid).spent == 0


def test_submission_counts_pending_reservations(runtime: Runtime) -> None:
    pid = project_id(runtime, budget=0.5)
    runtime.queue.submit("api", "other", {"estimated_cost": 0.4}, project_id=pid)
    with pytest.raises(BudgetExceeded):
        submit_character_sheet(runtime, pid, "Ada", "A fictional character")
    assert len(runtime.queue.list(project_id=pid)) == 1


def test_submission_requires_bounded_human_budget_approval(runtime: Runtime) -> None:
    pid = project_id(runtime)
    runtime.settings.confirm_threshold = 0.1
    with pytest.raises(ConfirmationRequired):
        submit_character_sheet(runtime, pid, "Ada", "A fictional character")
    with runtime.session_factory() as session:
        approve_budget(session, session.get(Project, pid), 0.25, "human")
        session.commit()
    submit_character_sheet(runtime, pid, "Ada", "A fictional character")
    with pytest.raises(ConfirmationRequired):
        submit_character_sheet(runtime, pid, "Bea", "Another fictional character")
    assert len(runtime.queue.list(project_id=pid)) == 1


def test_generated_character_remains_unusable_until_human_confirmation(runtime: Runtime) -> None:
    pid = project_id(runtime)
    job_id = submit_character_sheet(runtime, pid, "Ada", "An original illustrated botanist")
    job = runtime.queue.claim_next("api")
    assert job is not None
    images = FakeImageHandler()
    result = make_sheet_handler(runtime, images)(job)
    assert images.calls == [job_id]
    assert result["cost"] == 0.25
    with runtime.session_factory() as session:
        character = session.get(Character, result["character_id"])
        assert character is not None
        assert character.name == "Ada"
        assert character.fixed_appearance_prompt == "An original illustrated botanist"
        assert character.consent_confirmed is False
        assert character.consent_confirmed_by is None
        assert character.consent_confirmed_at is None
        assert character.reference_images["front"].startswith(f"assets/characters/{character.id}/")
        assert (runtime.settings.data_dir / character.reference_images["front"]).read_bytes() == PNG
        assert session.get(Project, pid).character_id is None
        library = AssetLibrary(session, AssetStorage(runtime.settings.data_dir))
        with pytest.raises(ConsentRequiredError):
            library.require_usable(character)
        library.confirm_consent(character, "human")
        session.commit()
        assert library.require_usable(character) is character
    assert runtime.queue.get(job_id).result == result


def test_retry_after_crash_reuses_persisted_asset_and_image(runtime: Runtime) -> None:
    pid = project_id(runtime)
    job_id = submit_character_sheet(runtime, pid, "Ada", "A fictional character")
    images = FakeImageHandler()
    handler = make_sheet_handler(runtime, images)
    first_attempt = runtime.queue.claim_next("api")
    assert first_attempt is not None
    first_result = handler(first_attempt)
    # Simulate a crash after the handler commits, before queue.complete runs.
    assert runtime.queue.recover_stale(force=True) == 1
    second_attempt = runtime.queue.claim_next("api")
    assert second_attempt is not None and second_attempt.attempts == 2
    assert handler(second_attempt) == first_result
    runtime.queue.complete(job_id, first_result, claim_token=second_attempt.claim_token)
    assert images.calls == [job_id]
    with runtime.session_factory() as session:
        assert len(list(session.scalars(select(Character)))) == 1
    assert runtime.queue.get(job_id).status == JobStatus.SUCCEEDED


@pytest.mark.parametrize("field", ["references", "context", "inputs"])
def test_handler_rejects_reference_media_before_generation(runtime: Runtime, field: str) -> None:
    pid = project_id(runtime)
    submit_character_sheet(runtime, pid, "Ada", "A fictional character")
    job = runtime.queue.claim_next("api")
    assert job is not None
    if field == "references":
        job.payload["recipe"]["reference_images"] = {"front": "uploads/source.png"}
    else:
        job.payload[field] = {"image": "uploads/source.png"}
    images = FakeImageHandler()
    with pytest.raises(ValueError, match="without reference media"):
        make_sheet_handler(runtime, images)(job)
    assert images.calls == []


@pytest.mark.parametrize("name,prompt", [(" ", "Fictional character"), ("Ada", " ")])
def test_submission_rejects_empty_character_details(
    runtime: Runtime, name: str, prompt: str
) -> None:
    pid = project_id(runtime)
    with pytest.raises(ValueError, match="required"):
        submit_character_sheet(runtime, pid, name, prompt)
    assert runtime.queue.list(project_id=pid) == []
