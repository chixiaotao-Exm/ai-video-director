"""OpenAI Chat Completions adapter with optional relay support."""

from __future__ import annotations

import importlib
import json
from copy import deepcopy
from typing import Any

from .base import ChatResponse, ProviderConfigurationError, ProviderResponseError, ToolCall


class OpenAIProvider:
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
                raise ProviderConfigurationError("Set AVD_OPENAI_API_KEY before using OpenAI")
            try:
                sdk = importlib.import_module("openai")
            except ImportError as exc:
                raise ProviderConfigurationError("Install avd[llm] to use OpenAI") from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = sdk.OpenAI(**kwargs)
        return self._client

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        converted = deepcopy(messages)
        for message in converted:
            if "tool_calls" in message:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call["arguments"]),
                        },
                    }
                    for call in message["tool_calls"]
                ]
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": converted,
            "max_completion_tokens": self.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tools
            ]
        result = self._get_client().chat.completions.create(**kwargs)
        message = result.choices[0].message
        usage = result.usage
        response = ChatResponse(
            content=message.content or "",
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            stop_reason=result.choices[0].finish_reason,
        )
        try:
            response.tool_calls = [
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=json.loads(call.function.arguments),
                )
                for call in (message.tool_calls or [])
            ]
        except (ValueError, TypeError) as exc:
            raise ProviderResponseError(
                "Invalid tool arguments in completed OpenAI response", response
            ) from exc
        return response


__all__ = ["OpenAIProvider"]
