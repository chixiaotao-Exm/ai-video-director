"""Director conversation loop with durable history and bounded tool execution."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.llm.providers import ChatResponse
from avd.state.models import Project

from .models import ChatMessage

if TYPE_CHECKING:
    from avd.runtime import Runtime


SYSTEM_PROMPT = """You are the AI Video Director, the user's long-term creative partner and the
only agent that talks to them. Always reply in the user's language (default: Simplified Chinese).
Be conversational and concrete. Project state and tool results are the source of truth.

How a project normally flows (conversation first, forms optional):
1. The user uploads a reference video. The application analyzes it automatically and then
   asks you to report. Your report: what the video is, its hook, structure shot by shot,
   pacing, camera language, music/voice style, and why it works. Then propose 2-3 concrete
   directions for an original video in a similar style and ask what the user wants.
2. Discuss freely. Use web_search whenever current facts help (trends, platform norms,
   references, techniques, tools, models). Never pretend to know recent facts; search.
3. When a character and voice are needed, explain what to prepare (photos with a front view,
   1-3 minutes of clean voice audio) and point the user to the Assets page. Consent for a real
   face or voice must be confirmed by the user in the Assets page; never confirm it yourself.
4. Write the script/storyboard when the user is ready, then first frames, then the full video.

Checkpoints (structure, script, frames, final) gate generation. When the user's latest message
clearly approves one ("可以", "就这样", "通过", "开始生成"...), call approve_checkpoint with their
exact words, then continue. If approval is ambiguous, ask. Never approve on your own initiative.

A reference video supplies structure and style only: never copy its identity, clone its
performer, or reproduce its spoken lines verbatim. Do not add an AI-generated watermark unless
the user asks. The user does not use budgets; don't discuss money unless asked.

You can check work yourself: use check_jobs to see what is queued, running, finished or
failed (with errors and outputs) before answering questions about progress, and list_library
for characters, voices and media clips (separated tracks live there, not in jobs). When something
worth showing is ready (frames, clips, voice, the final video, separated tracks), send it to
the user with send_media so it appears in the chat like a message in a messaging app. The user
may send you images, audio or video; images are shown to you, other files arrive as paths.

Never ask the user to come back and check progress; that is your job. Whenever you
queue background work, immediately call assign_watcher on those jobs with what to do when they
finish. The watcher reports back to you without the user doing anything, and you then send the
results and the next step. You and your worker sub-agents can talk to each other. delegate_task starts a worker
(research, checking assets, comparing options) while you keep talking to the user. It may
ask you mid-task; that arrives as [子Agent「…」问你]. Answer with message_subagent so the
conversation continues. If the question needs the user, ask them first, then pass their
decision back with message_subagent. You can also message an idle or waiting worker
yourself. A finished report arrives as [子Agent「…」汇报] or [系统通知]: tell the user what
matters. Sub-agents do not talk to the user or to each other.

Keep the live mind map current with update_mindmap. The user watches it update while you
talk, so it should show your chain of thought: insights from the reference (parent
"reference"), the creative directions you propose (options), what the user picked
(decision, status chosen; rejected ones status rejected), open questions, and next tasks.
Link each node to the node it follows from. Update it in the same turn as the conversation,
usually once per reply, with short labels and the detail in note.

For changes, update only affected recipes (update_shot), inspect plan_rebuild, and reuse valid
versions. Generation tools submit jobs and return job ids; report queued/running state and do
not claim queued work finished. A tool error is information: fix inputs or explain and ask.
"""

ROUND_LIMIT_MESSAGE = (
    "I reached this turn's tool-call limit. The completed steps and job ids are saved; "
    "send another message to continue or check project progress."
)

PERFORMANCE_PROMPT = """You are the AI Video Director for a performance_remix project.
Reply in the user's language, normally Simplified Chinese. Tools and persisted state are truth.

Preserve the selected source's choreography, lyrics, melody, accompaniment, audio/video offsets
and performance clock. The uploaded performer supplies motion and song, not the target identity.
Independently select the user's consented target character and singing voice from list_library.
Never substitute script writing, ordinary TTS, a new composition, slowed motion or the original
singer when the requested singing model is missing. Report the missing requirement explicitly.
Real-person consent is confirmed by the user in Assets; never grant it yourself.

Use configure_performance with only changed fields; keep saved inputs and valid versions.
Use get_performance_readiness before proposing generation. prepare_performance queues only
deterministic source preparation. After showing the configured source interval, selected target
assets and scene, wait for explicit human performance_inputs approval, quoting the user's latest
exact approving words through approve_checkpoint. preview_performance optionally queues scene
and singing references. Those references alone cannot satisfy performance_preview review.
Use preview_performance_video to generate the complete configured preview interval, at most
10 seconds, including motion, converted singing and the final MP4. Show the real outputs with
send_media; never call a queued job finished. Only after the user reviews that actual video
can performance_preview be approved and generate_performance continue the approved work.
performance_final records the user's review of the actual final output. Longer intervals
require the explicit segmented generation implementation; never silently shorten an interval.
Use plan_performance_segments after source preparation to inspect a proposed longer interval
without editing the saved preview. Its frame/sample plan does not execute models or prove
long-video readiness. Explain any short-tail or context contract limits to the user.
Questions, negative instructions, system events and task completion notices are not approval.
Never invent a checkpoint approval, consent, model version or a completed result. Keep the
existing budget and queue guards; no tool may grant its own budget confirmation.

Use check_jobs, list_versions and get_project_status to inspect progress. After queuing work,
assign_watcher to follow the returned jobs and report results. Use send_media for images,
singing audio and final MP4. Keep update_mindmap to concrete decisions and next steps.
For a scene change, update only scene_prompt and reuse prepared motion and singing vocals;
for voice changes, reuse visual results when their true dependencies are unchanged.
Use web_search when current facts are needed. Do not add a watermark unless the user asks.
"""


class DirectorAgent:
    """Run the configured director through short, independently committed steps."""

    def __init__(self, runtime: Runtime, max_rounds: int = 12) -> None:
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds < 1:
            raise ValueError("max_rounds must be a positive integer")
        self.runtime = runtime
        self.max_rounds = max_rounds

    @staticmethod
    def _project(session: Session, project_id: str) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise LookupError(f"Project {project_id!r} does not exist")
        return project

    def history(self, project_id: str) -> list[dict[str, Any]]:
        """Return isolated canonical messages in their original insertion order."""

        with self.runtime.session_factory() as session:
            self._project(session, project_id)
            rows = session.scalars(
                select(ChatMessage)
                .where(ChatMessage.project_id == project_id)
                .order_by(ChatMessage.id)
            )
            return [deepcopy(row.message) for row in rows]

    def transcript(self, project_id: str) -> list[dict[str, Any]]:
        """History for display: each message plus its time (``at``)."""

        with self.runtime.session_factory() as session:
            self._project(session, project_id)
            rows = session.scalars(
                select(ChatMessage)
                .where(ChatMessage.project_id == project_id)
                .order_by(ChatMessage.id)
            )
            return [
                {
                    **deepcopy(row.message),
                    "at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    def _append(self, project_id: str, message: dict[str, Any]) -> None:
        with self.runtime.session_factory() as session:
            self._project(session, project_id)
            session.add(ChatMessage(project_id=project_id, message=deepcopy(message)))
            session.commit()

    def _respond(
        self,
        project_id: str,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        *,
        job_id: str | None = None,
    ) -> ChatResponse:
        with self.runtime.session_factory() as session:
            project = self._project(session, project_id)
            try:
                return self.runtime.router(session, project, job_id=job_id).chat(
                    role="director", messages=messages, tools=schemas
                )
            finally:
                # Completed provider work remains billable even if a later step
                # raises. Release the transaction before any tool opens a session.
                session.commit()

    def _provider_view(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Turn stored attachments into model input: images are shown, other media named."""
        root = self.runtime.settings.data_dir.expanduser().resolve()
        view: list[dict[str, Any]] = []
        for message in self._paired_event_messages(messages):
            attachments = message.get("attachments") or []
            clean = {
                key: value
                for key, value in message.items()
                if key not in {"attachments", "event_id"}
            }
            if attachments and message["role"] == "user":
                notes = [
                    f"[{item['kind']}: {item['path']}]"
                    for item in attachments
                    if item["kind"] != "image"
                ]
                text = str(message.get("content") or "")
                if notes:
                    text = (text + "\n" if text else "") + "用户发送的文件：" + " ".join(notes)
                parts: list[dict[str, Any]] = [{"type": "text", "text": text or "（图片）"}]
                parts.extend(
                    {"type": "image", "path": str(root / item["path"])}
                    for item in attachments
                    if item["kind"] == "image" and (root / item["path"]).is_file()
                )
                clean["content"] = parts if len(parts) > 1 else text
            view.append(clean)
        return view

    @staticmethod
    def _paired_event_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # A later user turn can precede a tool reply recovered after a crash. Keep
        # chronological storage intact; providers require each reply beside its
        # assistant call. Include the event id because providers may reuse ids in
        # independent turns.
        replies = {
            (message["event_id"], message["tool_call_id"]): message
            for message in messages
            if message.get("event_id") is not None and message.get("role") == "tool"
        }
        calls = {
            (message["event_id"], call["id"])
            for message in messages
            if message.get("event_id") is not None and message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
        }
        ordered = []
        for message in messages:
            event_id = message.get("event_id")
            if message.get("role") == "tool" and (event_id, message.get("tool_call_id")) in calls:
                continue
            ordered.append(message)
            if event_id is not None and message.get("role") == "assistant":
                ordered.extend(
                    replies[(event_id, call["id"])]
                    for call in message.get("tool_calls") or []
                    if (event_id, call["id"]) in replies
                )
        return ordered

    def chat(
        self,
        project_id: str,
        text: str,
        *,
        event: bool = False,
        event_id: str | None = None,
        attachments: list[dict[str, str]] | None = None,
    ) -> str:
        """Persist a user turn, execute requested tools, and return the final reply.

        ``event`` marks an application notice (e.g. analysis finished) sent on the
        user's behalf; it cannot approve checkpoints. ``event_id`` identifies a
        durable background notification so retries resume its saved tool results.
        """

        if not isinstance(text, str) or (not text.strip() and not attachments):
            raise ValueError("Chat text must be a non-empty string")
        if event_id is not None and (
            not event or not isinstance(event_id, str) or not event_id.strip()
        ):
            raise ValueError("event_id requires an application event and a non-empty id")

        from .turnlock import director_turn

        with director_turn(self.runtime.settings.data_dir, project_id):
            return self._chat_locked(
                project_id, text, event=event, event_id=event_id, attachments=attachments
            )

    def _chat_locked(
        self,
        project_id: str,
        text: str,
        *,
        event: bool,
        event_id: str | None,
        attachments: list[dict[str, str]] | None,
    ) -> str:
        # Tools also use the runtime; import here to keep package imports acyclic.
        from .tools import DirectorTools

        with self.runtime.session_factory() as session:
            mode = self._project(session, project_id).mode
        system = (
            PERFORMANCE_PROMPT
            if mode == "performance_remix"
            else SYSTEM_PROMPT + (ANIME_PROMPT if mode == "anime_sequel" else "")
        )
        history = self.history(project_id)
        existing = next(
            (
                message
                for message in history
                if event_id is not None
                and message.get("event_id") == event_id
                and message.get("role") == "user"
            ),
            None,
        )
        if existing is not None and existing.get("content") != text:
            raise ValueError("An application event id cannot be reused for a different notice")
        if existing is not None:
            completed = next(
                (
                    message
                    for message in reversed(history)
                    if message.get("event_id") == event_id
                    and message.get("role") == "assistant"
                    and not message.get("tool_calls")
                ),
                None,
            )
            if completed is not None:
                return str(completed.get("content") or "")
        messages = [{"role": "system", "content": system}, *history]
        user_message: dict[str, Any] = {"role": "user", "content": text}
        if event:
            user_message["event"] = True
        if event_id is not None:
            user_message["event_id"] = event_id
        if attachments:
            user_message["attachments"] = attachments
        if existing is None:
            self._append(project_id, user_message)
            messages.append(user_message)
        else:
            assert event_id is not None
            self._recover_event_tools(project_id, event_id, messages)
        tools = DirectorTools(self.runtime, project_id, None if event else text, job_id=event_id)
        schemas = tools.schemas(mode=mode)

        for _ in range(self.max_rounds):
            response = self._respond(
                project_id, self._provider_view(messages), schemas, job_id=event_id
            )
            assistant_message = response.as_message()
            if event_id is not None:
                assistant_message["event_id"] = event_id
            self._append(project_id, assistant_message)
            messages.append(assistant_message)
            if not response.tool_calls:
                return response.content

            for call in response.tool_calls:
                saved = next(
                    (
                        message
                        for message in reversed(messages[:-1])
                        if event_id is not None
                        and message.get("event_id") == event_id
                        and message.get("role") == "tool"
                        and message.get("tool_call_id") == call.id
                    ),
                    None,
                )
                if saved is not None:
                    previous_calls = [
                        previous
                        for message in messages[:-1]
                        if message.get("event_id") == event_id
                        and message.get("role") == "assistant"
                        for previous in message.get("tool_calls") or []
                        if previous["id"] == call.id
                    ]
                    prior = previous_calls[-1] if previous_calls else {}
                    result = (
                        saved["content"]
                        if prior.get("name") == call.name
                        and prior.get("arguments", {}) == call.arguments
                        else "Tool error: this event tool-call id was already used for a different request; no work was repeated."
                    )
                else:
                    try:
                        result = tools.execute(call.name, call.arguments)
                    except Exception as exc:
                        # Keep call/result pairing even for unavailable tools so
                        # the next model turn can recover.
                        detail = " ".join(str(exc).split())[:1000]
                        result = f"Tool error ({call.name}): {type(exc).__name__}: {detail}"
                tool_message: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                }
                if event_id is not None:
                    tool_message["event_id"] = event_id
                sent = tools.take_attachments()
                if sent:
                    # Shown to the user as media bubbles; the model sees the text result.
                    tool_message["attachments"] = sent
                self._append(project_id, tool_message)
                messages.append(tool_message)

        limit_message: dict[str, Any] = {"role": "assistant", "content": ROUND_LIMIT_MESSAGE}
        if event_id is not None:
            limit_message["event_id"] = event_id
        self._append(project_id, limit_message)
        return ROUND_LIMIT_MESSAGE

    def _recover_event_tools(
        self, project_id: str, event_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Pair interrupted calls without guessing whether their side effect ran.

        Tool execution and recording its reply cannot share every tool's DB/file
        transaction. A missing durable reply is therefore an uncertain outcome,
        not permission to rerun a generation or other side effect on job retry.
        """
        pending: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            if message.get("event_id") != event_id or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            following = messages[index + 1 :]
            replies = {
                item.get("tool_call_id")
                for item in following
                if item.get("role") == "tool" and item.get("event_id") == event_id
            }
            for call in calls:
                if call["id"] not in replies:
                    pending.append(
                        {
                            "role": "tool",
                            "tool_call_id": call["id"],
                            "name": call["name"],
                            "content": (
                                "The previous attempt was interrupted before this tool's result "
                                "was saved. Its outcome is unknown and it was not replayed. "
                                "Read project/job state before deciding whether further work is needed."
                            ),
                            "event_id": event_id,
                        }
                    )
        for result in pending:
            self._append(project_id, result)
            messages.append(result)


ANIME_PROMPT = """
ANIME SEQUEL MODE. This project continues an anime series as its next episode.
- The analysis includes a story_bible (characters, world, art style, story so far, open
  threads, cliffhanger). Your report should retell the episode, introduce the cast and
  their relationships, describe the art style, and point out the hooks a sequel can use.
- Pitch 2-3 sequel episode directions, each with a logline, 3-5 beats, which threads it pays
  off, and a new cliffhanger. Ask which to develop; then expand it into an episode outline.
- Characters here are drawn, fictional characters. Build their reference sheets with
  create_character(generate_sheet=true) from the story bible's appearance notes in the same
  anime style. Keep each character on-model across shots.
- If the series belongs to someone else, mention once that a sequel is fan work for personal
  use unless the user holds the rights; do not refuse.
- Mind map: one branch per character and per open thread under "reference", the pitched
  directions as options under "root", and the chosen episode beats as tasks.
"""

ANALYSIS_READY_EVENT = (
    "[系统通知] 参考视频已自动分析完成。请调用 get_project_status 读取分析结果，"
    "向我做一份完整的分析报告，然后提出 2-3 个原创方向并问我想怎么做。"
)
ANALYSIS_FAILED_EVENT = (
    "[系统通知] 参考视频自动分析失败：{error}\n请用通俗的话告诉我出了什么问题、我该怎么处理。"
)


__all__ = ["ANALYSIS_FAILED_EVENT", "ANALYSIS_READY_EVENT", "DirectorAgent", "SYSTEM_PROMPT"]
