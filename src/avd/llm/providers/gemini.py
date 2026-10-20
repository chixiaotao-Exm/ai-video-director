"""Google GenAI adapter with inline video and large-video File API support."""

from __future__ import annotations

from copy import deepcopy
import importlib
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from .base import ChatResponse, ProviderConfigurationError, ToolCall

logger = logging.getLogger(__name__)


class GeminiProvider:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: Any = None,
        file_timeout: float = 600.0,
        max_output_tokens: int = 4096,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = client
        self.file_timeout = file_timeout
        self.max_output_tokens = max_output_tokens

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise ProviderConfigurationError("Set AVD_GEMINI_API_KEY before using Gemini")
            try:
                sdk = importlib.import_module("google.genai")
            except ImportError as exc:
                raise ProviderConfigurationError("Install avd[llm] to use Gemini") from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["http_options"] = {"base_url": self.base_url}
            self._client = sdk.Client(**kwargs)
        return self._client

    def _video_part(self, block: dict[str, Any], uploaded: list[str]) -> dict[str, Any]:
        video = Path(block["path"])
        mime_type = block.get("mime_type", "video/mp4")
        if video.stat().st_size < 20_000_000:
            return {"inline_data": {"mime_type": mime_type, "data": video.read_bytes()}}
        client = self._get_client()
        remote = client.files.upload(file=str(video), config={"mime_type": mime_type})
        uploaded.append(remote.name)
        deadline = time.monotonic() + self.file_timeout
        while getattr(remote.state, "name", str(remote.state)) == "PROCESSING":
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Gemini video processing timed out for {video.name}")
            time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
            remote = client.files.get(name=remote.name)
        if getattr(remote.state, "name", str(remote.state)) != "ACTIVE":
            raise RuntimeError(f"Gemini could not process video {video.name}: {remote.state}")
        return {"file_data": {"mime_type": mime_type, "file_uri": remote.uri}}

    def _parts(self, content: Any, uploaded: list[str]) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"text": content}]
        parts = []
        for block in content:
            if block.get("type") == "text":
                parts.append({"text": block["text"]})
            elif block.get("type") == "video":
                parts.append(self._video_part(block, uploaded))
            else:
                parts.append(deepcopy(block))
        return parts

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        client = self._get_client()
        uploaded: list[str] = []
        try:
            contents: list[dict[str, Any]] = []
            system: list[str] = []
            for message in messages:
                role = message["role"]
                if role == "system":
                    system.append(str(message.get("content", "")))
                    continue
                if role == "tool":
                    parts = [{"function_response": {
                        "name": message["name"], "response": {"result": message.get("content", "")}
                    }}]
                else:
                    parts = self._parts(message.get("content", ""), uploaded)
                    parts.extend(
                        {"function_call": {"name": call["name"], "args": call["arguments"]}}
                        for call in message.get("tool_calls", [])
                    )
                contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})
            config: dict[str, Any] = {"max_output_tokens": self.max_output_tokens}
            if system:
                config["system_instruction"] = "\n\n".join(system)
            if tools:
                config["tools"] = [{"function_declarations": [
                    {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool["input_schema"]}
                    for tool in tools
                ]}]
            result = client.models.generate_content(model=model, contents=contents, config=config)
            candidates = result.candidates or []
            parts = candidates[0].content.parts if candidates and candidates[0].content else []
            usage = result.usage_metadata
            return ChatResponse(
                content="".join(part.text for part in parts if getattr(part, "text", None)),
                tool_calls=[
                    ToolCall(id=getattr(part.function_call, "id", None) or str(uuid.uuid4()), name=part.function_call.name, arguments=dict(part.function_call.args or {}))
                    for part in parts if getattr(part, "function_call", None)
                ],
                input_tokens=(usage.prompt_token_count or 0) if usage else 0,
                output_tokens=(usage.candidates_token_count or 0) if usage else 0,
                stop_reason=str(candidates[0].finish_reason) if candidates else None,
            )
        finally:
            for name in uploaded:
                try:
                    client.files.delete(name=name)
                except Exception:
                    logger.warning("Could not delete uploaded Gemini file %s", name, exc_info=True)


__all__ = ["GeminiProvider"]
