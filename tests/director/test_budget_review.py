from pathlib import Path
from types import SimpleNamespace

import pytest

from avd.config import ModelPrice, Settings
from avd.director import DirectorAgent
from avd.director.review import approve_budget
from avd.llm import ChatResponse, ConfirmationRequired, FakeProvider
from avd.llm.providers.base import ProviderResponseError
from avd.llm.providers.openai import OpenAIProvider
from avd.runtime import Runtime
from avd.state.models import Project
from avd.state.repo import ProjectRepo


def test_approved_chat_can_proceed_but_each_call_rechecks_remaining_amount(tmp_path: Path) -> None:
    provider = FakeProvider([ChatResponse(content="Done", output_tokens=10)])
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        llm_max_output_tokens=2000,
        model_prices={"claude-opus-5-5": ModelPrice(output_per_million=1000)},
    )
    runtime = Runtime(settings, providers={"anthropic": provider})
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Chat", budget_limit=100)
        project_id = project.id
        session.commit()
    with pytest.raises(ConfirmationRequired):
        DirectorAgent(runtime).chat(project_id, "Hello")
    assert not provider.calls
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        approve_budget(session, project, 2.001, "human")
        session.commit()
    assert DirectorAgent(runtime).chat(project_id, "Proceed") == "Done"
    with pytest.raises(ConfirmationRequired):
        DirectorAgent(runtime).chat(project_id, "Another call")
    assert len(provider.calls) == 1
    runtime.close()


def test_queued_confirmed_boolean_cannot_bypass_live_approval(tmp_path: Path) -> None:
    fake = FakeProvider(["unused"])
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path), providers={"openai": fake})
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Queued", budget_limit=10)
        project_id = project.id
        session.commit()
    job_id = runtime.queue.submit("api", "write_script", {"confirmed": True}, project_id=project_id)
    with runtime.session_factory() as session:
        project = session.get(Project, project_id)
        with pytest.raises(ConfirmationRequired):
            runtime.router(session, project, job_id=job_id).chat("script", [], estimated_cost=2)
    assert not fake.calls
    runtime.close()


def test_malformed_tool_response_keeps_usage_in_ledger(tmp_path: Path) -> None:
    bad = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call",
                            function=SimpleNamespace(name="get_budget", arguments="{broken"),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
    )
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: bad))
    )
    runtime = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            model_prices={"gpt-6-astra": ModelPrice(input_per_million=1, output_per_million=2)},
        )
    )
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Malformed", budget_limit=10)
        router = runtime.router(session, project)
        router.providers["openai"] = OpenAIProvider(client=sdk)
        with pytest.raises(ProviderResponseError):
            router.chat("script", [])
        entries = router.ledger.list(project.id)
        assert len(entries) == 1
        assert (entries[0].input_tokens, entries[0].output_tokens) == (100, 50)
        assert project.spent == pytest.approx(0.0002)
        session.commit()
    runtime.close()
