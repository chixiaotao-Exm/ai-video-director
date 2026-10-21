from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from avd.config import Settings
from avd.director import subagents
from avd.director.subagents import SubAgent, on_job_finished, run_worker_turn
from avd.director.tools import DirectorTools
from avd.jobs import JobQueue
from avd.jobs.models import Job, JobStatus
from avd.llm import CostLedger, LLMRouter
from avd.llm.providers import ChatResponse, FakeProvider, ToolCall
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.repo import ProjectRepo


@pytest.fixture
def runtime(tmp_path: Path) -> Any:
    settings = Settings(_env_file=None, data_dir=tmp_path)
    engine = create_engine(settings.database_url)
    init_db(engine)
    factory = create_session_factory(engine)
    queue = JobQueue(factory)
    fake = FakeProvider([])
    current = SimpleNamespace(settings=settings, session_factory=factory, queue=queue, fake=fake)

    def router(session: Any, project: Any, **_: Any) -> LLMRouter:
        return LLMRouter(
            settings, providers={"openai": fake}, ledger=CostLedger(session), project=project
        )

    current.router = router
    with factory() as session:
        project = ProjectRepo(session).create_project("p", budget_limit=None)
        session.commit()
        current.project_id = project.id
    current.tools = DirectorTools(current, current.project_id)
    return current


def _finish(runtime: Any, job_id: str, status: JobStatus = JobStatus.SUCCEEDED) -> Job:
    with runtime.session_factory() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.status = status
        session.commit()
        session.expunge(job)
        return job


def _reports(runtime: Any) -> list[str]:
    with runtime.session_factory() as session:
        jobs = session.scalars(select(Job).where(Job.job_type == "director_report")).all()
        return [job.payload["text"] for job in jobs]


def test_watcher_reports_once_all_watched_jobs_finish(runtime: Any) -> None:
    first = runtime.queue.submit("api", "image_gen", {}, project_id=runtime.project_id)
    second = runtime.queue.submit("api", "image_gen", {}, project_id=runtime.project_id)
    result = json.loads(
        runtime.tools.execute(
            "assign_watcher", {"instructions": "把首帧发给用户", "name": "盯首帧"}
        )
    )
    assert result["watching"] == 2
    on_job_finished(runtime, _finish(runtime, first))
    assert _reports(runtime) == []
    on_job_finished(runtime, _finish(runtime, second, JobStatus.FAILED))
    [report] = _reports(runtime)
    assert "盯首帧" in report and "1/2" in report and "把首帧发给用户" in report
    with runtime.session_factory() as session:
        assert session.scalar(select(SubAgent.status)) == "done"


def test_batch_end_reports_without_a_watcher(runtime: Any) -> None:
    job = runtime.queue.submit("cpu", "subtitles", {}, project_id=runtime.project_id)
    on_job_finished(runtime, _finish(runtime, job))
    [report] = _reports(runtime)
    assert report.startswith("[系统通知]")
    # A second finished job while that report is still queued does not duplicate it.
    other = runtime.queue.submit("cpu", "subtitles", {}, project_id=runtime.project_id)
    on_job_finished(runtime, _finish(runtime, other))
    assert len(_reports(runtime)) == 1


def test_conversation_jobs_never_trigger_reports(runtime: Any) -> None:
    job = runtime.queue.submit(
        "api", "director_report", {"text": "x"}, project_id=runtime.project_id
    )
    on_job_finished(runtime, _finish(runtime, job))
    assert len(_reports(runtime)) == 1  # only the one submitted above


def test_watcher_needs_jobs_of_this_project(runtime: Any) -> None:
    with pytest.raises(ValueError, match="no queued or running"):
        runtime.tools.execute("assign_watcher", {"instructions": "x"})
    with pytest.raises(ValueError, match="not in this project"):
        runtime.tools.execute("assign_watcher", {"instructions": "x", "job_ids": ["nope"]})


def test_worker_researches_with_tools_and_reports(runtime: Any) -> None:
    runtime.fake.responses = [
        ChatResponse(tool_calls=[ToolCall(id="c1", name="list_library", arguments={})]),
        ChatResponse(content="素材库是空的，建议先上传人物照片。"),
    ]
    created = json.loads(
        runtime.tools.execute("delegate_task", {"name": "调研员", "task": "看看素材库里有什么"})
    )
    assert created["status"] == "working"
    reply = run_worker_turn(runtime, created["agent_id"])
    assert reply == "素材库是空的，建议先上传人物照片。"
    reports = _reports(runtime)
    assert any("调研员" in text and "素材库是空的" in text for text in reports)
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent is not None and agent.status == "idle"
        assert [item["role"] for item in agent.transcript] == [
            "user",
            "assistant",
            "tool",
            "assistant",
        ]
    # Follow-up messages queue another turn; stopping prevents further work.
    follow = json.loads(
        runtime.tools.execute(
            "message_subagent", {"agent_id": created["agent_id"], "message": "再查一下声音"}
        )
    )
    assert follow["status"] == "working"
    runtime.tools.execute("stop_subagent", {"agent_id": created["agent_id"]})
    assert run_worker_turn(runtime, created["agent_id"]) == "stopped"


def test_worker_can_ask_the_director_and_wait_for_the_reply(runtime: Any) -> None:
    runtime.fake.responses = [
        ChatResponse(
            tool_calls=[
                ToolCall(
                    id="c1", name="message_director", arguments={"message": "用暖色还是冷色？"}
                )
            ]
        )
    ]
    created = json.loads(runtime.tools.execute("delegate_task", {"name": "配色", "task": "定颜色"}))
    reply = run_worker_turn(runtime, created["agent_id"])
    assert reply == "用暖色还是冷色？"
    [report] = _reports(runtime)
    assert "问你" in report and "用暖色还是冷色" in report and "message_subagent" in report
    tool_names = [item["name"] for item in runtime.fake.calls[0]["tools"]]
    assert "message_director" in tool_names and "rebuild" not in tool_names
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent is not None and agent.status == "waiting"
        assert agent.transcript[-1]["content"] == "已发给导演，等待回复：用暖色还是冷色？"
    follow = json.loads(
        runtime.tools.execute(
            "message_subagent", {"agent_id": created["agent_id"], "message": "用暖色"}
        )
    )
    assert follow["status"] == "working"
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent is not None
        assert agent.transcript[-1] == {
            "role": "user",
            "content": "用暖色",
            "at": agent.transcript[-1]["at"],
        }


def test_worker_cannot_use_write_tools(runtime: Any) -> None:
    runtime.fake.responses = [
        ChatResponse(tool_calls=[ToolCall(id="c1", name="rebuild", arguments={})]),
        ChatResponse(content="做不了"),
    ]
    created = json.loads(runtime.tools.execute("delegate_task", {"name": "x", "task": "y"}))
    run_worker_turn(runtime, created["agent_id"])
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent is not None
        tool_result = next(item for item in agent.transcript if item["role"] == "tool")
        assert "not available to sub-agents" in tool_result["content"]
    assert subagents.WORKER_TOOLS


def test_question_notification_retry_does_not_call_model_or_skip_wait(runtime: Any, monkeypatch):
    runtime.fake.responses = [
        ChatResponse(
            tool_calls=[
                ToolCall(id="ask", name="message_director", arguments={"message": "选哪个场景？"})
            ]
        )
    ]
    created = json.loads(runtime.tools.execute("delegate_task", {"name": "场景", "task": "选场景"}))
    original = runtime.queue.submit
    failures = 0

    def submit(*args, **kwargs):
        nonlocal failures
        if args[1] == "director_report" and failures == 0:
            failures += 1
            raise RuntimeError("queue unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.queue, "submit", submit)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        run_worker_turn(runtime, created["agent_id"])
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent.status == "waiting" and agent.pending_report
    assert run_worker_turn(runtime, created["agent_id"]) == "选哪个场景？"
    assert run_worker_turn(runtime, created["agent_id"]) == "选哪个场景？"
    assert len(runtime.fake.calls) == 1
    assert len(_reports(runtime)) == 1
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent.status == "waiting" and agent.pending_report is None


@pytest.mark.parametrize("terminal", [JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_worker_job_releases_working_status(runtime: Any, terminal: JobStatus):
    created = json.loads(runtime.tools.execute("delegate_task", {"name": "x", "task": "y"}))
    on_job_finished(runtime, _finish(runtime, created["job_id"], terminal))
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, created["agent_id"])
        assert agent.status == ("failed" if terminal == JobStatus.FAILED else "stopped")
    if terminal == JobStatus.FAILED:
        follow = json.loads(
            runtime.tools.execute(
                "message_subagent", {"agent_id": created["agent_id"], "message": "重试"}
            )
        )
        assert follow["status"] == "working"


def test_stopped_worker_does_not_call_model_again_between_rounds(runtime: Any, monkeypatch):
    created = json.loads(runtime.tools.execute("delegate_task", {"name": "x", "task": "y"}))
    runtime.fake.responses = [
        ChatResponse(tool_calls=[ToolCall(id="read", name="list_library", arguments={})]),
        ChatResponse(content="不应调用"),
    ]
    original = DirectorTools.execute

    def execute(self, name, arguments):
        result = original(self, name, arguments)
        if name == "list_library":
            runtime.tools.execute("stop_subagent", {"agent_id": created["agent_id"]})
        return result

    monkeypatch.setattr(DirectorTools, "execute", execute)
    assert run_worker_turn(runtime, created["agent_id"]) == "stopped"
    assert len(runtime.fake.calls) == 1
    assert not _reports(runtime)


def test_existing_database_adds_pending_report_field(tmp_path: Path):
    from sqlalchemy import inspect

    engine = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    init_db(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE subagents DROP COLUMN pending_report")
    init_db(engine)
    assert "pending_report" in {c["name"] for c in inspect(engine).get_columns("subagents")}
    init_db(engine)
    engine.dispose()
