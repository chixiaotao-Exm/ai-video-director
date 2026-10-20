"""Sub-agents the director assigns: watchers that follow jobs, and workers that do tasks.

* A **watcher** follows a set of jobs without using a model. When they have all
  finished it reports to the director, who then tells the user and sends results.
* A **worker** is a small agent with its own model and read-only tools. It runs a
  task in the background, reports to the director, and can be messaged again.

Reports reach the director as application events (``director_report`` jobs), so the
director speaks first, the way a teammate would in a messaging app.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.jobs.models import Job, JobStatus
from avd.state.models import Base, Project

if TYPE_CHECKING:
    from avd.runtime import Runtime

# Jobs that belong to the conversation itself never trigger job reports.
CONVERSATION_JOBS = frozenset({"director_report", "subagent_turn"})
ACTIVE = (JobStatus.QUEUED, JobStatus.RUNNING)
WORKER_TOOLS = (
    "web_search",
    "get_project_status",
    "check_jobs",
    "list_library",
    "list_versions",
    "plan_rebuild",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SubAgent(Base):
    __tablename__ = "subagents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # watcher | worker
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    # watching | working | idle | done | failed | stopped
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    job_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    transcript: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def agent_data(agent: SubAgent) -> dict[str, Any]:
    return {
        "id": agent.id,
        "name": agent.name,
        "kind": agent.kind,
        "status": agent.status,
        "instructions": agent.instructions,
        "job_ids": agent.job_ids,
        "result": agent.result,
        "transcript": [
            item for item in agent.transcript if item.get("role") in {"user", "assistant"}
        ][-20:],
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _active_job_ids(session: Session, project_id: str) -> list[str]:
    return list(
        session.scalars(
            select(Job.id).where(
                Job.project_id == project_id,
                Job.status.in_(ACTIVE),
                Job.job_type.not_in(CONVERSATION_JOBS),
            )
        )
    )


def create_watcher(
    session: Session, project: Project, *, name: str, instructions: str, job_ids: list[str] | None
) -> SubAgent:
    ids = job_ids or _active_job_ids(session, project.id)
    known = set(
        session.scalars(select(Job.id).where(Job.project_id == project.id, Job.id.in_(ids)))
    )
    missing = [job_id for job_id in ids if job_id not in known]
    if missing:
        raise ValueError(f"These jobs are not in this project: {', '.join(missing[:5])}")
    if not ids:
        raise ValueError("There are no queued or running jobs to watch")
    agent = SubAgent(
        project_id=project.id,
        name=name,
        kind="watcher",
        instructions=instructions,
        status="watching",
        job_ids=ids,
    )
    session.add(agent)
    session.flush()
    return agent


def submit_worker_turn(runtime: Runtime, session: Session, agent: SubAgent, message: str) -> str:
    agent.transcript = [
        *agent.transcript,
        {"role": "user", "content": message, "at": _now().isoformat()},
    ]
    agent.status = "working"
    agent.updated_at = _now()
    session.flush()
    return runtime.queue.submit(
        "api",
        "subagent_turn",
        {"agent_id": agent.id},
        project_id=agent.project_id,
        model_key=runtime.settings.llm_route("subagent").model,
        max_attempts=2,
        session=session,
    )


def create_worker(
    runtime: Runtime, session: Session, project: Project, *, name: str, task: str
) -> tuple[SubAgent, str]:
    agent = SubAgent(
        project_id=project.id, name=name, kind="worker", instructions=task, status="working"
    )
    session.add(agent)
    session.flush()
    job_id = submit_worker_turn(runtime, session, agent, task)
    return agent, job_id


def _report(runtime: Runtime, project_id: str, text: str) -> None:
    runtime.queue.submit(
        "api",
        "director_report",
        {"project_id": project_id, "text": text},
        project_id=project_id,
        model_key=runtime.settings.director_model,
        max_attempts=2,
    )


def _job_line(job: Job) -> str:
    error = (job.error or "").strip().splitlines()[-1:] if job.status == JobStatus.FAILED else []
    suffix = f"（{error[0][:160]}）" if error else ""
    return f"{job.job_type} {job.status.value}{suffix}"


def on_job_finished(runtime: Runtime, job: Job) -> None:
    """Fire watchers whose jobs are all done; otherwise report when a batch ends."""
    if not job.project_id or job.job_type in CONVERSATION_JOBS:
        return
    if job.status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
        return  # a failed attempt that will be retried
    reports: list[str] = []
    with runtime.session_factory() as session:
        watchers = session.scalars(
            select(SubAgent).where(
                SubAgent.project_id == job.project_id,
                SubAgent.kind == "watcher",
                SubAgent.status == "watching",
            )
        ).all()
        for watcher in watchers:
            jobs = session.scalars(select(Job).where(Job.id.in_(watcher.job_ids))).all()
            if any(item.status in ACTIVE for item in jobs):
                continue
            succeeded = sum(item.status == JobStatus.SUCCEEDED for item in jobs)
            summary = f"{succeeded}/{len(jobs)} 个成功：" + "；".join(
                _job_line(item) for item in jobs
            )
            watcher.status = "done"
            watcher.result = summary
            watcher.updated_at = _now()
            reports.append(
                f"[子Agent「{watcher.name}」汇报] 你让我盯的任务都结束了。{summary}\n"
                f"你交代的事：{watcher.instructions}\n"
                "请用 check_jobs 看结果，把新生成的内容用 send_media 发给用户，并说明下一步。"
            )
        batch_over = not _active_job_ids(session, job.project_id)
        pending_report = session.scalar(
            select(Job.id).where(
                Job.project_id == job.project_id,
                Job.job_type == "director_report",
                Job.status == JobStatus.QUEUED,
            )
        )
        session.commit()
    for text in reports:
        _report(runtime, job.project_id, text)
    if not reports and batch_over and pending_report is None:
        _report(
            runtime,
            job.project_id,
            "[系统通知] 这个项目的后台任务都结束了。请用 check_jobs 看看哪些成功、哪些失败，"
            "把新生成的内容用 send_media 发给用户；失败的说明原因和处理办法。",
        )


WORKER_PROMPT = """You are a sub-agent named "{name}", assigned by the AI Video Director of a
video project. Do the task below with your tools, then reply with a concise report for the
director (Simplified Chinese): findings, sources if you searched, and anything the director
must decide. You cannot talk to the user or change the project; the director does that.

Task: {task}"""


def run_worker_turn(runtime: Runtime, agent_id: str) -> str:
    """One background turn of a worker sub-agent; reports its answer to the director."""
    from .tools import DirectorTools

    with runtime.session_factory() as session:
        agent = session.get(SubAgent, agent_id)
        if agent is None:
            raise KeyError("Sub-agent not found")
        if agent.status == "stopped":
            return "stopped"
        project_id, name, task = agent.project_id, agent.name, agent.instructions
        transcript = list(agent.transcript)
    tools = DirectorTools(runtime, project_id)
    schemas = [schema for schema in tools.schemas() if schema["name"] in WORKER_TOOLS]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": WORKER_PROMPT.format(name=name, task=task)},
        *({key: value for key, value in item.items() if key != "at"} for item in transcript),
    ]
    reply = "（没有得出结论）"
    for _ in range(10):
        with runtime.session_factory() as session:
            project = session.get(Project, project_id)
            assert project is not None
            try:
                response = runtime.router(session, project).chat(
                    "subagent", messages, tools=schemas
                )
            finally:
                session.commit()
        message = response.as_message()
        messages.append(message)
        transcript.append({**message, "at": _now().isoformat()})
        if not response.tool_calls:
            reply = response.content or reply
            break
        for call in response.tool_calls:
            try:
                if call.name not in WORKER_TOOLS:
                    raise ValueError(f"Tool not available to sub-agents: {call.name}")
                result = tools.execute(call.name, call.arguments)
            except Exception as exc:
                result = f"Tool error ({call.name}): {' '.join(str(exc).split())[:600]}"
            tool_message = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": call.name,
                "content": result,
            }
            messages.append(tool_message)
            transcript.append({**tool_message, "at": _now().isoformat()})
    with runtime.session_factory() as session:
        agent = session.get(SubAgent, agent_id)
        if agent is not None and agent.status != "stopped":
            agent.transcript = transcript
            agent.result = reply
            agent.status = "idle"
            agent.updated_at = _now()
        session.commit()
    _report(
        runtime,
        project_id,
        f"[子Agent「{name}」汇报] {reply}\n（子Agent编号 {agent_id}；需要追问就用 message_subagent。"
        "把对用户有用的部分转告用户。）",
    )
    return reply


def summary(agents: list[SubAgent]) -> str:
    return json.dumps(
        [
            {
                "id": agent.id,
                "name": agent.name,
                "kind": agent.kind,
                "status": agent.status,
                "instructions": agent.instructions,
                "result": (agent.result or "")[:600],
            }
            for agent in agents
        ],
        ensure_ascii=False,
    )


__all__ = [
    "CONVERSATION_JOBS",
    "SubAgent",
    "agent_data",
    "create_watcher",
    "create_worker",
    "on_job_finished",
    "run_worker_turn",
    "submit_worker_turn",
    "summary",
]
