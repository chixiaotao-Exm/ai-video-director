"""Scriptable, completely offline provider used by tests and local demos."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from .base import ChatResponse


class FakeProvider:
    def __init__(self, responses: Iterable[ChatResponse | str | Exception] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        self.calls.append(deepcopy({"model": model, "messages": messages, "tools": tools}))
        if not self.responses:
            raise RuntimeError("FakeProvider has no scripted responses left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return ChatResponse(content=response)
        return response.model_copy(deep=True)


__all__ = ["FakeProvider"]
