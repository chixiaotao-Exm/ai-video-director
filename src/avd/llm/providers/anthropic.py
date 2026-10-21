"""Anthropic Messages adapter, including canonical tool-result messages."""

from __future__ import annotations

import base64
import importlib
import mimetypes
from copy import deepcopy
from pathlib import Path
from typing import Any

from .base import ChatResponse, ProviderConfigurationError, ToolCall

# Block types that must be replayed verbatim on the next request of a tool loop.
_REPLAYED_BLOCKS = frozenset(
    {"thinking", "redacted_thinking", "server_tool_use", "web_search_tool_result", "text"}
)


def _block_dict(block: Any) -> dict[str, Any]:
    if hasattr(block, "model_dump"):
        return dict(block.model_dump(mode="json", exclude_none=True))
    return {key: value for key, value in vars(block).items() if value is not None}


def _image_block(item: dict[str, Any]) -> dict[str, Any]:
    path = Path(item["path"])
    media_type = item.get("mime_type") or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        },
    }


def _content(content: Any) -> Any:
    if not isinstance(content, list):
        return deepcopy(content)
    converted: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image" and "path" in item:
            converted.append(_image_block(item))
        elif isinstance(item, dict) and item.get("type") == "video":
            raise ProviderConfigurationError("Anthropic models cannot watch video; send frames")
        else:
            converted.append(deepcopy(item))
    return converted


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
            kwargs: dict[str, Any] = {"api_key": self.api_key, "max_retries": 0}
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
                result = {
                    "type": "tool_result",
                    "tool_use_id": message["tool_call_id"],
                    "content": content,
                }
                # Parallel tool results belong in one user message.
                previous = converted[-1] if converted else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                    and all(block.get("type") == "tool_result" for block in previous["content"])
                ):
                    previous["content"].append(result)
                else:
                    converted.append({"role": "user", "content": [result]})
                continue
            raw = message.get("provider_blocks", {}).get("anthropic")
            if role == "assistant" and raw:
                blocks = deepcopy(raw)
                blocks.extend(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["arguments"],
                    }
                    for call in message.get("tool_calls", [])
                )
                converted.append({"role": role, "content": blocks})
            elif message.get("tool_calls"):
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
                converted.append({"role": role, "content": _content(content)})
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
        replay = [_block_dict(block) for block in result.content if block.type in _REPLAYED_BLOCKS]
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
            provider_blocks={"anthropic": replay} if replay else {},
        )

    def search(self, model: str, query: str) -> ChatResponse:
        """Answer a query with Anthropic's server-side web search tool."""

        result = self._get_client().messages.create(
            model=model,
            max_tokens=self.max_output_tokens,
            # The basic variant works through relays that lack server code execution.
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
            messages=[{"role": "user", "content": query}],
        )
        sources: list[str] = []
        for block in result.content:
            if block.type == "web_search_tool_result" and isinstance(block.content, list):
                for item in block.content:
                    url = getattr(item, "url", None)
                    if url and url not in sources:
                        sources.append(url)
        text = "".join(block.text for block in result.content if block.type == "text")
        if sources:
            text += "\n\nSources:\n" + "\n".join(f"- {url}" for url in sources[:10])
        return ChatResponse(
            content=text,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            stop_reason=result.stop_reason,
        )


__all__ = ["AnthropicProvider"]
