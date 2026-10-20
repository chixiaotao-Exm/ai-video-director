"""Queue prompt-only character sheets and keep generated assets pending human review."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from avd.director.review import budget_is_confirmed
from avd.jobs import Job
from avd.jobs.queue import ClaimLostError
from avd.llm.budget import BudgetExceeded, BudgetGuard
from avd.state.models import Project
from avd.state.paths import relative_path

from .library import AssetLibrary
from .storage import AssetStorage

if TYPE_CHECKING:
    from avd.runtime import Runtime


def submit_character_sheet(
    runtime: Runtime, project_id: str, name: str, appearance_prompt: str
) -> str:
    """Submit a fictional character described in text, without reference media."""
    name = name.strip()
    appearance_prompt = appearance_prompt.strip()
    if not name or not appearance_prompt:
        raise ValueError("Character name and appearance prompt are required")

    with runtime.session_factory() as session:
        # Reservations and submission share the lock used by the rebuild scheduler.
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        project = session.get(Project, project_id)
        if project is None:
            raise KeyError(f"Project {project_id} was not found")
        estimate = runtime.settings.job_estimated_costs.get("image_gen", 0.0)
        reserved = runtime._reserved(session, project_id)
        BudgetGuard(runtime.settings.confirm_threshold).check(
            project,
            estimate,
            confirmed=budget_is_confirmed(session, project, estimate + reserved),
        )
        if (
            project.budget_limit is not None
            and project.spent + reserved + estimate > project.budget_limit
        ):
            raise BudgetExceeded(
                "Pending work plus this character sheet exceeds the project budget"
            )
        output_dir = (
            f"projects/{project_id}/character-sheet-{uuid.uuid4().hex}/1-{uuid.uuid4().hex}"
        )
        job_id = runtime.queue.submit(
            "api",
            "character_sheet",
            {
                "name": name,
                "appearance_prompt": appearance_prompt,
                "data_dir": str(runtime.settings.data_dir.expanduser().resolve()),
                "output_dir": output_dir,
                "recipe": {
                    "character_sheet": True,
                    "appearance_prompt": appearance_prompt,
                    "_execution": {"image_model": runtime.settings.image_model},
                },
                "context": {},
                "inputs": {},
                "estimated_cost": estimate,
            },
            project_id=project_id,
            node_id=None,
            model_key=runtime.settings.image_model,
            session=session,
        )
        session.commit()
        return job_id


def _saved_job(session: Session, job: Job) -> Job:
    saved = session.get(Job, job.id)
    if saved is None:
        raise LookupError("Character sheet job no longer exists")
    if saved.claim_token != job.claim_token:
        raise ClaimLostError(f"Job {job.id} is no longer owned by this attempt")
    return saved


def make_sheet_handler(
    runtime: Runtime, image_handler: Callable[[Job], dict[str, Any]]
) -> Callable[[Job], dict[str, Any]]:
    """Wrap image generation with atomic asset creation and durable retry recovery."""

    def execute(job: Job) -> dict[str, Any]:
        with runtime.session_factory() as session:
            saved = _saved_job(session, job)
            if saved.result is not None:
                return deepcopy(saved.result)

        recipe = job.payload.get("recipe", {})
        if (
            recipe.get("character_sheet") is not True
            or recipe.get("reference_images")
            or job.payload.get("context")
            or job.payload.get("inputs")
        ):
            raise ValueError("Character sheets require a text prompt without reference media")
        result = deepcopy(image_handler(job))
        image_relative = relative_path(result.get("outputs", {}).get("image"))
        root = Path(job.payload["data_dir"]).expanduser().resolve()
        image_path = (root / image_relative).resolve()
        output_dir = (root / relative_path(job.payload["output_dir"])).resolve()
        if not output_dir.is_relative_to(root) or not image_path.is_relative_to(output_dir):
            raise ValueError("Character sheet image must stay inside the job output directory")

        with runtime.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            saved = _saved_job(session, job)
            if saved.result is not None:
                return deepcopy(saved.result)
            library = AssetLibrary(session, AssetStorage(root))
            character = library.create_character(
                job.payload["name"],
                {"front": str(image_path)},
                job.payload["appearance_prompt"],
            )
            result.update(
                character_id=character.id,
                summary="Character sheet ready for human review and consent confirmation",
            )
            # Commit the asset and recovery marker before the worker marks the job complete.
            saved.result = deepcopy(result)
            session.commit()
        return result

    return execute


__all__ = ["make_sheet_handler", "submit_character_sheet"]
