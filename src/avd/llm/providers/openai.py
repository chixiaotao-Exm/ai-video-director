"""OpenAI Responses and Chat Completions adapters with optional relay support."""

from __future__ import annotations

import base64
import importlib
import json
import mimetypes
from copy import deepcopy
from pathlib import Path
from typing import Any

from avd.jobs.failures import classify_failure

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


def _responses_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    converted = []
    for item in _content(content):
        if item.get("type") == "text":
            converted.append({"type": "input_text", "text": item["text"]})
        elif item.get("type") == "image_url":
            image = item["image_url"]
            converted.append(
                {
                    "type": "input_image",
                    "image_url": image["url"],
                    **{key: image[key] for key in ("detail",) if key in image},
                }
            )
        else:
            converted.append(deepcopy(item))
    return converted


def _response_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {key: _response_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_response_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _response_value(item) for key, item in vars(value).items() if item is not None}
    return value


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            converted.append(
                {
                    "type": "function_call_output",
                    "call_id": message["tool_call_id"],
                    "output": message.get("content", ""),
                }
            )
            continue
        raw = message.get("provider_blocks", {}).get("openai_responses")
        if role == "assistant" and raw:
            # Native output includes reasoning, calls and assistant messages in order.
            # Replaying canonical calls as well would execute each call twice.
            converted.extend(deepcopy(raw))
            continue
        content = message.get("content", "")
        calls = message.get("tool_calls") or []
        if content or not calls:
            converted.append({"role": role, "content": _responses_content(content)})
        converted.extend(
            {
                "type": "function_call",
                "call_id": call["id"],
                "name": call["name"],
                "arguments": json.dumps(call["arguments"]),
            }
            for call in calls
        )
    return converted


def _responses_envelope(result: Any) -> ChatResponse:
    output = [_response_value(item) for item in (getattr(result, "output", None) or [])]
    replay = [
        item for item in output if item.get("type") in {"reasoning", "function_call", "message"}
    ]
    usage = getattr(result, "usage", None)
    return ChatResponse(
        content="".join(
            part["text"]
            for item in output
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ),
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        stop_reason=getattr(result, "status", None),
        provider_blocks={"openai_responses": replay} if replay else {},
    )


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
            kwargs: dict[str, Any] = {"api_key": self.api_key, "max_retries": 0}
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
        if self.label == "OpenAI" and model.startswith(("gpt-6-astra", "gpt-6.1-sol")):
            return self._responses_chat(model, messages, tools)
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

    def _responses_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> ChatResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "input": _responses_input(messages),
            "max_output_tokens": self.max_output_tokens,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": deepcopy(tool["input_schema"]),
                    # Preserve the existing schemas' optional arguments.
                    "strict": False,
                }
                for tool in tools
            ]
        result = self._stream_responses(kwargs)
        output = [_response_value(item) for item in result.output]
        response = _responses_envelope(result)
        if response.stop_reason in {"failed", "incomplete", "cancelled"}:
            raise ProviderResponseError("OpenAI Responses did not complete", response)
        try:
            response.tool_calls = [
                ToolCall(
                    id=item["call_id"],
                    name=item["name"],
                    arguments=json.loads(item["arguments"]),
                )
                for item in output
                if item.get("type") == "function_call"
            ]
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderResponseError(
                "Invalid tool arguments in completed OpenAI Responses reply", response
            ) from exc
        return response

    def _stream_responses(self, kwargs: dict[str, Any]) -> Any:
        latest = None
        try:
            with self._get_client().responses.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "response", None) is not None:
                        latest = event.response
                    event_type = getattr(event, "type", None)
                    if event_type in {"response.failed", "response.incomplete", "error"}:
                        upstream = (
                            event if event_type == "error" else getattr(latest, "error", None)
                        )
                        details = _response_value(upstream)
                        failure = classify_failure(
                            json.dumps(details)
                            + "\n"
                            + (str(details.get("message", "")) if isinstance(details, dict) else "")
                        )
                        raise ProviderResponseError(
                            "OpenAI Responses stream did not complete",
                            _responses_envelope(latest),
                            failure=failure,
                        )
                    if event_type == "response.completed":
                        if getattr(latest, "status", None) != "completed":
                            raise ProviderResponseError(
                                "OpenAI Responses stream did not complete",
                                _responses_envelope(latest),
                            )
                        # This SDK event contains the complete accumulated reply.
                        # Do not require a clean transport EOF after terminal success.
                        return latest
                raise ProviderResponseError(
                    "OpenAI Responses stream ended without completion",
                    _responses_envelope(latest),
                )
        except ProviderResponseError:
            raise
        except Exception as exc:
            if latest is None:
                # Preserve HTTP/connection errors before any response arrives.
                raise
            raise ProviderResponseError(
                "OpenAI Responses stream was interrupted",
                _responses_envelope(latest),
                failure=classify_failure(exc),
            ) from exc

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
