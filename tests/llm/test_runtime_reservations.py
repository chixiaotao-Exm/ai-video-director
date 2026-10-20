import json
from pathlib import Path

import pytest

from avd.assets.sheets import submit_character_sheet
from avd.config import Settings
from avd.director.tools import DirectorTools
from avd.llm import BudgetExceeded, CostEntry, FakeProvider
from avd.runtime import Runtime
from avd.state.models import Project


def test_completed_sheet_cannot_free_budget_before_callback_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(["should not be called"])
    runtime = Runtime(
        Settings(_env_file=None, data_dir=tmp_path, job_estimated_costs={"image_gen": 0.6}),
        providers={"openai": provider},
    )
    try:
        with runtime.session_factory() as session:
            project = Project(title="Reservation handoff", budget_limit=1)
            session.add(project)
            session.commit()
            project_id = project.id
        job_id = submit_character_sheet(
            runtime, project_id, "Original character", "An illustration"
        )
        claimed = runtime.queue.claim_next("api")
        # An offline success may omit the live-provider marker. Its reported cost
        # still occupies the budget until the idempotent completion callback runs.
        finished = runtime.queue.complete(job_id, {"cost": 0.6}, claim_token=claimed.claim_token)
        for accounted in (False, True):
            if accounted:
                runtime.on_finished(finished)
                runtime.on_finished(finished)
            with runtime.session_factory() as session:
                project = session.get(Project, project_id)
                assert project.spent == pytest.approx(0.6 if accounted else 0)
                assert runtime._reserved(session, project_id) == pytest.approx(
                    0 if accounted else 0.6
                )
                with pytest.raises(BudgetExceeded):
                    runtime.router(session, project).chat("script", [], estimated_cost=0.6)
            with pytest.raises(BudgetExceeded):
                submit_character_sheet(runtime, project_id, "Another", "An illustration")
            budget = json.loads(DirectorTools(runtime, project_id).execute("get_budget", {}))
            assert budget["remaining"] == pytest.approx(0.4)
        with runtime.session_factory() as session:
            assert session.query(CostEntry).filter_by(job_id=job_id).count() == 1
        assert not provider.calls
    finally:
        runtime.close()
