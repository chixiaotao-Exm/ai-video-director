"""Manual image smoke with a private persisted ledger; never auto-runs paid work."""

import uuid

from avd.assets.sheets import submit_character_sheet
from avd.config import Settings
from avd.jobs import JobStatus, Worker
from avd.runtime import Runtime
from avd.state.repo import ProjectRepo


def main() -> None:
    settings = Settings()
    prompt = settings.smoke_image_prompt
    if not settings.openai_api_key or not prompt:
        print("Skipped: set AVD_OPENAI_API_KEY and AVD_SMOKE_IMAGE_PROMPT.")
        return
    if settings.smoke_image_reference:
        print(
            "Skipped: use the consented asset workflow for reference edits; this smoke generates a fictional sheet."
        )
        return
    directory = settings.data_dir.resolve() / "smoke" / "image" / uuid.uuid4().hex
    directory.mkdir(parents=True)
    settings.database_url = f"sqlite:///{(directory / 'state.db').as_posix()}"
    runtime = Runtime(settings)
    try:
        with runtime.session_factory() as session:
            project = ProjectRepo(session).create_project("Image smoke", budget_limit=1)
            project_id = project.id
            session.commit()
        job_id = submit_character_sheet(runtime, project_id, "Smoke fictional character", prompt)
        Worker(
            runtime.queue, "api", runtime.build_registry(), on_finished=runtime.on_finished
        ).run_once()
        job = runtime.queue.get(job_id)
        assert job is not None
        if job.status != JobStatus.SUCCEEDED:
            raise RuntimeError(job.error or "Image smoke did not complete")
        print(f"Generated asset awaiting review: {job.result}")
        print(f"Ledger: {directory / 'state.db'}")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
