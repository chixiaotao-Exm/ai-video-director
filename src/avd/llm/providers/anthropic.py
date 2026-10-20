"""Anthropic Messages adapter, including canonical tool-result messages."""

from __future__ import annotations

import importlib
from copy import deepcopy
from typing import Any

from .base import ChatResponse, ProviderConfigurationError, ToolCall


class AnthropicProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: Any = None,
        max_output_tokens: int = 4096,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = client
        self.max_output_tokens = max_output_tokens

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ProviderConfigurationError("Set AVD_ANTHROPIC_API_KEY before using Anthropic")
            try:
                sdk = importlib.import_module("anthropic")
            except ImportError as exc:
                raise ProviderConfigurationError("Install avd[llm] to use Anthropic") from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = sdk.Anthropic(**kwargs)
        return self._client

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        converted: list[dict[str, Any]] = []
        system: list[str] = []
        for message in messages:
            role = message["role"]
            content = message.get("content", "")
            if role == "system":
                system.append(str(content))
                continue
            if role == "tool":
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message["tool_call_id"],
                                "content": content,
                            }
                        ],
                    }
                )
                continue
            if message.get("tool_calls"):
                blocks = [{"type": "text", "text": content}] if content else []
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                    for call in message["tool_calls"]
                )
                converted.append({"role": role, "content": blocks})
            else:
                converted.append({"role": role, "content": deepcopy(content)})
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_tokens": self.max_output_tokens,
        }
        if system:
            kwargs["system"] = "\n\n".join(system)
        if tools:
            kwargs["tools"] = deepcopy(tools)
        result = self._get_client().messages.create(**kwargs)
        return ChatResponse(
            content="".join(block.text for block in result.content if block.type == "text"),
            tool_calls=[
                ToolCall(id=block.id, name=block.name, arguments=block.input)
                for block in result.content
                if block.type == "tool_use"
            ],
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            stop_reason=result.stop_reason,
        )


__all__ = ["AnthropicProvider"]
