from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from avd.config import ModelPrice, Settings
from avd.director import ChatMessage, DirectorAgent
from avd.director.agent import ROUND_LIMIT_MESSAGE, SYSTEM_PROMPT
from avd.llm import ChatResponse, CostEntry, FakeProvider
from avd.llm.providers import ToolCall
from avd.runtime import Runtime
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@dataclass
class AgentHarness:
    runtime: Runtime
    provider: FakeProvider
    project_id: str
    other_project_id: str


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[AgentHarness]:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        confirm_threshold=100,
        model_prices={"claude-opus-5-5": ModelPrice(input_per_million=2, output_per_million=4)},
    )
    engine = create_engine(settings.database_url, connect_args={"timeout": 0.1})
    init_db(engine)
    session_factory = create_session_factory(engine)
    provider = FakeProvider()
    runtime = Runtime(settings, session_factory=session_factory, providers={"anthropic": provider})
    with session_factory() as session:
        repo = ProjectRepo(session)
        project = repo.create_project("Director project", budget_limit=100)
        other_project = repo.create_project("Separate project", budget_limit=100)
        session.commit()
        project_id, other_project_id = project.id, other_project.id
    yield AgentHarness(runtime, provider, project_id, other_project_id)
    engine.dispose()


def test_scripted_tool_loop_preserves_messages_and_usage(harness: AgentHarness) -> None:
    calls = [
        ToolCall(id="status-call", name="get_project_status"),
        ToolCall(id="budget-call", name="get_budget"),
    ]
    harness.provider.responses.extend(
        [
            ChatResponse(
                content="I will check progress and budget.",
                tool_calls=calls,
                input_tokens=100,
                output_tokens=50,
            ),
            ChatResponse(
                content="Ready for your sample video.", input_tokens=200, output_tokens=100
            ),
        ]
    )
    agent = DirectorAgent(harness.runtime)

    assert agent.chat(harness.project_id, "What is the project status?") == (
        "Ready for your sample video."
    )
    history = agent.history(harness.project_id)
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    assert history[1]["tool_calls"] == [call.model_dump() for call in calls]
    assert [message["tool_call_id"] for message in history[2:4]] == ["status-call", "budget-call"]
    assert all(isinstance(message["content"], str) for message in history)
    assert harness.provider.calls[1]["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history[:-1],
    ]
    assert "get_budget" in {tool["name"] for tool in harness.provider.calls[0]["tools"]}
    with harness.runtime.session_factory() as session:
        entries = list(session.scalars(select(CostEntry)))
        assert len(entries) == 2
        assert {entry.role for entry in entries} == {"director"}
        project = session.get(Project, harness.project_id)
        assert project is not None
        assert project.spent == pytest.approx(0.0012)


def test_history_survives_new_agent_and_is_isolated_by_project(harness: AgentHarness) -> None:
    harness.provider.responses.extend(["First answer", "Second answer", "Other answer"])
    DirectorAgent(harness.runtime).chat(harness.project_id, "First question")
    agent = DirectorAgent(harness.runtime)
    assert agent.chat(harness.project_id, "Second question") == "Second answer"
    assert harness.provider.calls[1]["messages"][1:] == [
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
    ]
    agent.chat(harness.other_project_id, "Other question")
    assert harness.provider.calls[2]["messages"][1:] == [
        {"role": "user", "content": "Other question"}
    ]
    copied_history = agent.history(harness.project_id)
    copied_history[0]["content"] = "Changed outside the database"
    assert agent.history(harness.project_id)[0]["content"] == "First question"


def test_history_uses_sequence_even_when_timestamps_tie(harness: AgentHarness) -> None:
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with harness.runtime.session_factory() as session:
        for text in ("first", "second", "third"):
            session.add(
                ChatMessage(
                    project_id=harness.project_id,
                    message={"role": "user", "content": text},
                    created_at=timestamp,
                )
            )
        session.commit()
    assert [
        message["content"] for message in DirectorAgent(harness.runtime).history(harness.project_id)
    ] == ["first", "second", "third"]


@pytest.mark.parametrize("text", ["", " ", "\n\t"])
def test_empty_turn_does_not_call_provider(harness: AgentHarness, text: str) -> None:
    agent = DirectorAgent(harness.runtime)
    with pytest.raises(ValueError, match="non-empty"):
        agent.chat(harness.project_id, text)
    assert agent.history(harness.project_id) == []
    assert harness.provider.calls == []


def test_unknown_project_does_not_call_provider(harness: AgentHarness) -> None:
    agent = DirectorAgent(harness.runtime)
    with pytest.raises(LookupError, match="does not exist"):
        agent.chat("missing-project", "Hello")
    with pytest.raises(LookupError, match="does not exist"):
        agent.history("missing-project")
    assert harness.provider.calls == []


@pytest.mark.parametrize("max_rounds", [0, -1, True, 1.5])
def test_round_limit_must_be_positive_integer(harness: AgentHarness, max_rounds: Any) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        DirectorAgent(harness.runtime, max_rounds=max_rounds)


def test_unknown_and_invalid_tools_are_reported_and_recoverable(harness: AgentHarness) -> None:
    harness.provider.responses.extend(
        [
            ChatResponse(
                tool_calls=[
                    ToolCall(id="unknown", name="invented_tool"),
                    ToolCall(id="invalid", name="write_script", arguments={}),
                ]
            ),
            "Please provide a brief for the script.",
        ]
    )
    agent = DirectorAgent(harness.runtime)
    assert agent.chat(harness.project_id, "Write a script") == (
        "Please provide a brief for the script."
    )
    results = [
        message for message in agent.history(harness.project_id) if message["role"] == "tool"
    ]
    assert len(results) == 2
    assert results[0]["content"].startswith("Tool error (invented_tool)")
    assert results[1]["content"].startswith("Tool error (write_script)")
    assert "brief" in results[1]["content"]


def test_loop_limit_preserves_tool_results_and_returns_saved_reply(harness: AgentHarness) -> None:
    harness.provider.responses.append(
        ChatResponse(tool_calls=[ToolCall(id="last-budget", name="get_budget")])
    )
    agent = DirectorAgent(harness.runtime, max_rounds=1)
    assert agent.chat(harness.project_id, "Check my budget") == ROUND_LIMIT_MESSAGE
    history = agent.history(harness.project_id)
    assert [message["role"] for message in history] == ["user", "assistant", "tool", "assistant"]
    assert history[-2]["tool_call_id"] == "last-budget"
    assert history[-1]["content"] == ROUND_LIMIT_MESSAGE
    assert len(harness.provider.calls) == 1


def test_paid_usage_survives_tool_failure_and_later_provider_failure(harness: AgentHarness) -> None:
    harness.provider.responses.extend(
        [
            ChatResponse(
                tool_calls=[ToolCall(id="bad-tool", name="invented_tool")],
                input_tokens=1000,
            ),
            RuntimeError("Scripted provider failure"),
        ]
    )
    agent = DirectorAgent(harness.runtime)
    with pytest.raises(RuntimeError, match="Scripted provider failure"):
        agent.chat(harness.project_id, "Attempt an action")
    assert [message["role"] for message in agent.history(harness.project_id)] == [
        "user",
        "assistant",
        "tool",
    ]
    with harness.runtime.session_factory() as session:
        assert len(list(session.scalars(select(CostEntry)))) == 1
        project = session.get(Project, harness.project_id)
        assert project is not None
        assert project.spent == pytest.approx(0.002)


def test_tool_can_write_in_own_session_after_paid_response(
    harness: AgentHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from avd.director.tools import DirectorTools

    def write_in_tool(self: DirectorTools, name: str, arguments: dict[str, Any]) -> str:
        with harness.runtime.session_factory() as session:
            project = session.get(Project, harness.project_id)
            assert project is not None
            assert project.spent == pytest.approx(0.002)
            project.title = "Updated inside the tool"
            session.commit()
        return "Queued job: job-123"

    monkeypatch.setattr(DirectorTools, "execute", write_in_tool)
    harness.provider.responses.extend(
        [
            ChatResponse(
                tool_calls=[ToolCall(id="queue", name="generate_frames")],
                input_tokens=1000,
            ),
            "Frame job job-123 is queued.",
        ]
    )
    agent = DirectorAgent(harness.runtime)
    assert agent.chat(harness.project_id, "Generate approved frames") == (
        "Frame job job-123 is queued."
    )
    assert agent.history(harness.project_id)[2]["content"] == "Queued job: job-123"
    with harness.runtime.session_factory() as session:
        project = session.get(Project, harness.project_id)
        assert project is not None and project.title == "Updated inside the tool"


def test_system_prompt_covers_human_decisions_and_nonblocking_jobs() -> None:
    prompt = SYSTEM_PROMPT.lower()
    for phrase in (
        "structure template",
        "user approves the script",
        "user approves the first frames",
        "user reviews the final video",
        "never self-approve",
        "consent_confirmed",
        "budget confirmation",
        "do not wait or repeatedly poll",
        "ai-generated",
    ):
        assert phrase in prompt
