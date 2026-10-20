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


SYSTEM_PROMPT = """You are the AI Video Director, the only agent that talks to the user.
Use project state and tool results as the source of truth. Explain progress concisely and
ask the user for decisions when needed. A sample video supplies structure and style only:
never copy its identity, clone its performer, or reproduce its spoken lines.

Follow these user review checkpoints in order:
1. Analyze the sample and let the user review the structure template before writing.
2. Create or select the user's character and voice. The user must approve the character
   sheet and explicitly confirm consent for face and voice use. Do not use an asset until
   its consent_confirmed flag is true; never infer consent or confirm it yourself.
3. Write the script/storyboard and pause for checkpoint 1: user approves the script.
4. Generate first frames and pause for checkpoint 2: user approves the first frames.
   This is the most important checkpoint; resolve visual changes before video generation.
5. After approval, generate video, voice, lip sync, subtitles, and final assembly. Pause
   for checkpoint 3: the user reviews the final video. Keep the AI-generated label.
Never self-approve a checkpoint, character sheet, consent, or a budget confirmation. Only
an explicit user decision or an approval recorded by the application authorizes it.

Check the project budget before paid work. Explain estimated spending and ask the user
before large batches or any action requiring budget confirmation. Never invent approval,
set a confirmation flag on your own, or bypass a refused budget or consent check.
For changes, update only affected recipes, inspect plan_rebuild, and reuse valid versions.
Generation tools submit jobs and return job ids. Report those ids and queued/running state;
do not wait or repeatedly poll for jobs in this turn, and never claim queued work finished.
Use get_project_status for progress on a subsequent request. A tool error is information:
correct invalid inputs when possible, or explain the issue and ask for the needed decision.
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

    def chat(self, project_id: str, text: str) -> str:
        """Persist a user turn, execute requested tools, and return the final reply."""

        if not isinstance(text, str) or not text.strip():
            raise ValueError("Chat text must be a non-empty string")

        # Tools also use the runtime; import here to keep package imports acyclic.
        from .tools import DirectorTools

        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *self.history(project_id)]
        user_message = {"role": "user", "content": text}
        self._append(project_id, user_message)
        messages.append(user_message)
        tools = DirectorTools(self.runtime, project_id)
        schemas = tools.schemas()

        for _ in range(self.max_rounds):
            response = self._respond(project_id, messages, schemas)
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
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result,
                }
                self._append(project_id, tool_message)
                messages.append(tool_message)

        self._append(project_id, {"role": "assistant", "content": ROUND_LIMIT_MESSAGE})
        return ROUND_LIMIT_MESSAGE


__all__ = ["DirectorAgent", "SYSTEM_PROMPT"]
