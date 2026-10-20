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
failed (with errors and outputs) before answering questions about progress. When something
worth showing is ready (frames, clips, voice, the final video, separated tracks), send it to
the user with send_media so it appears in the chat like a message in a messaging app. The user
may send you images, audio or video; images are shown to you, other files arrive as paths.

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
    ) -> ChatResponse:
        with self.runtime.session_factory() as session:
            project = self._project(session, project_id)
            try:
                return self.runtime.router(session, project).chat(
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
        for message in messages:
            attachments = message.get("attachments") or []
            clean = {key: value for key, value in message.items() if key != "attachments"}
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

    def chat(
        self,
        project_id: str,
        text: str,
        *,
        event: bool = False,
        attachments: list[dict[str, str]] | None = None,
    ) -> str:
        """Persist a user turn, execute requested tools, and return the final reply.

        ``event`` marks an application notice (e.g. analysis finished) sent on the
        user's behalf; it cannot approve checkpoints.
        """

        if not isinstance(text, str) or (not text.strip() and not attachments):
            raise ValueError("Chat text must be a non-empty string")

        # Tools also use the runtime; import here to keep package imports acyclic.
        from .tools import DirectorTools

        with self.runtime.session_factory() as session:
            mode = self._project(session, project_id).mode
        system = SYSTEM_PROMPT + (ANIME_PROMPT if mode == "anime_sequel" else "")
        messages = [{"role": "system", "content": system}, *self.history(project_id)]
        user_message: dict[str, Any] = {"role": "user", "content": text}
        if event:
            user_message["event"] = True
        if attachments:
            user_message["attachments"] = attachments
        self._append(project_id, user_message)
        messages.append(user_message)
        tools = DirectorTools(self.runtime, project_id, None if event else text)
        schemas = tools.schemas()

        for _ in range(self.max_rounds):
            response = self._respond(project_id, self._provider_view(messages), schemas)
            assistant_message = response.as_message()
            self._append(project_id, assistant_message)
            messages.append(assistant_message)
            if not response.tool_calls:
                return response.content

            for call in response.tool_calls:
                try:
                    result = tools.execute(call.name, call.arguments)
                except Exception as exc:
                    # Keep the tool-call/result pairing even for bad arguments or
                    # unavailable tools, so the next model turn can recover.
                    detail = " ".join(str(exc).split())[:1000]
                    result = f"Tool error ({call.name}): {type(exc).__name__}: {detail}"
                tool_message: dict[str, Any] = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                }
                sent = tools.take_attachments()
                if sent:
                    # Shown to the user as media bubbles; the model sees the text result.
                    tool_message["attachments"] = sent
                self._append(project_id, tool_message)
                messages.append(tool_message)

        self._append(project_id, {"role": "assistant", "content": ROUND_LIMIT_MESSAGE})
        return ROUND_LIMIT_MESSAGE


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
