"""OpenAI Chat Completions adapter with optional relay support."""

from __future__ import annotations

import base64
import importlib
import json
import mimetypes
from copy import deepcopy
from pathlib import Path
from typing import Any

from .base import ChatResponse, ProviderConfigurationError, ProviderResponseError, ToolCall


def _content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image" and "path" in item:
            path = Path(item["path"])
            media_type = item.get("mime_type") or mimetypes.guess_type(path.name)[0] or "image/jpeg"
            data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
            converted.append(
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}
            )
        elif isinstance(item, dict) and item.get("type") == "video":
            raise ProviderConfigurationError("OpenAI chat models cannot watch video; send frames")
        else:
            converted.append(item)
    return converted


class OpenAIProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: Any = None,
        max_output_tokens: int = 4096,
        label: str = "OpenAI",
    ) -> None:
        self.label = label
        self.api_key = api_key
        self.base_url = base_url
        self._client = client
        self.max_output_tokens = max_output_tokens

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ProviderConfigurationError(
                    f"Set the {self.label} API key in Settings before using {self.label}"
                )
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
            message.pop("provider_blocks", None)
            message.pop("event", None)
            message.pop("attachments", None)
            message["content"] = _content(message.get("content", ""))
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

    def search(self, model: str, query: str) -> ChatResponse:
        """Answer a query with the Responses API web_search tool, citing sources."""

        result = self._get_client().responses.create(
            model=model,
            input=query,
            tools=[{"type": "web_search"}],
            max_output_tokens=self.max_output_tokens,
        )
        sources: list[str] = []
        for item in result.output:
            for part in getattr(item, "content", None) or []:
                for note in getattr(part, "annotations", None) or []:
                    url = getattr(note, "url", None)
                    if url and url not in sources:
                        sources.append(url)
        text = result.output_text or ""
        if sources:
            text += "\n\nSources:\n" + "\n".join(f"- {url}" for url in sources[:10])
        usage = result.usage
        return ChatResponse(
            content=text,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            stop_reason=getattr(result, "status", None),
        )


__all__ = ["OpenAIProvider"]
