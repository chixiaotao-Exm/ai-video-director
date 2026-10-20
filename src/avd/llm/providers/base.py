"""Provider-neutral chat messages, tool calls, and usage."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    stop_reason: str | None = None

    def as_message(self) -> dict[str, Any]:
        """Return the canonical assistant message used by all providers."""

        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [call.model_dump() for call in self.tool_calls]
        return message


class Provider(Protocol):
    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse: ...


class ProviderConfigurationError(ValueError):
    """A live provider is missing its SDK, credentials, or route configuration."""


class ProviderResponseError(ValueError):
    """A completed response could not be normalized; usage still must be billed."""

    def __init__(self, message: str, response: ChatResponse) -> None:
        super().__init__(message)
        self.response = response


__all__ = ["ChatResponse", "Provider", "ProviderConfigurationError", "ToolCall"]
