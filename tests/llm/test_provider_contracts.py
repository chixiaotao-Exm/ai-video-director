"""Exercise live-adapter contracts with tiny SDK doubles and no SDK installation."""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, Field

from avd.config import ModelPrice, Settings
from avd.llm.ledger import CostLedger
from avd.llm.providers.base import ChatResponse, ToolCall
from avd.llm.providers.fake import FakeProvider
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.repo import ProjectRepo


class RecordingCall:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(deepcopy(kwargs))
        return self.response


TOOL = {
    "name": "get_project_status",
    "description": "Read project status.",
    "input_schema": {
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
}


def conversation() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Keep responses short."},
        {"role": "user", "content": "Read project p1."},
        ChatResponse(
            content="Checking.",
            tool_calls=[
                ToolCall(
                    id="call-1", name="get_project_status", arguments={"project_id": "p1"}
                )
            ],
        ).as_message(),
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "get_project_status",
            "content": '{"status":"clean"}',
        },
    ]


class ShotCount(BaseModel):
    count: int = Field(gt=0)


def test_anthropic_normalizes_tool_round_trip_and_usage() -> None:
    from avd.llm.providers.anthropic import AnthropicProvider

    create = RecordingCall(
        SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Ready."),
                SimpleNamespace(
                    type="tool_use", id="call-2", name="get_project_status", input={"project_id": "p2"}
                ),
            ],
            usage=SimpleNamespace(input_tokens=12, output_tokens=7),
            stop_reason="tool_use",
        )
    )
    provider = AnthropicProvider(client=SimpleNamespace(messages=SimpleNamespace(create=create)))
    messages = conversation()
    original = deepcopy(messages)
    result = provider.chat("claude-test", messages, [TOOL])

    assert result.content == "Ready."
    assert result.tool_calls == [
        ToolCall(id="call-2", name="get_project_status", arguments={"project_id": "p2"})
    ]
    assert (result.input_tokens, result.output_tokens, result.stop_reason) == (12, 7, "tool_use")
    sent = create.calls[0]
    assert sent["model"] == "claude-test"
    assert sent["system"] == "Keep responses short."
    assert sent["tools"] == [TOOL]
    assert sent["messages"][1]["content"][1] == {
        "type": "tool_use", "id": "call-1", "name": TOOL["name"], "input": {"project_id": "p1"}
    }
    assert sent["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": '{"status":"clean"}'}],
    }
    assert messages == original


def test_openai_normalizes_tool_only_reply_and_usage() -> None:
    from avd.llm.providers.openai import OpenAIProvider

    create = RecordingCall(
        SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id="call-2",
                        function=SimpleNamespace(name=TOOL["name"], arguments='{"project_id":"p2"}'),
                    )],
                ),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(prompt_tokens=17, completion_tokens=9),
        )
    )
    provider = OpenAIProvider(
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    )
    messages = conversation()
    original = deepcopy(messages)
    result = provider.chat("gpt-test", messages, [TOOL])

    assert result.content == ""
    assert result.tool_calls[0].arguments == {"project_id": "p2"}
    assert (result.input_tokens, result.output_tokens, result.stop_reason) == (17, 9, "tool_calls")
    sent = create.calls[0]
    assert sent["model"] == "gpt-test"
    assert sent["tools"][0]["function"]["parameters"] == TOOL["input_schema"]
    sent_call = sent["messages"][2]["tool_calls"][0]
    assert sent_call["type"] == "function"
    assert json.loads(sent_call["function"]["arguments"]) == {"project_id": "p1"}
    assert sent["messages"][3]["tool_call_id"] == "call-1"
    assert messages == original


def test_gemini_normalizes_system_tool_round_trip_and_usage() -> None:
    from avd.llm.providers.gemini import GeminiProvider

    generate = RecordingCall(
        SimpleNamespace(
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[
                    SimpleNamespace(text="Ready.", function_call=None),
                    SimpleNamespace(text=None, function_call=SimpleNamespace(
                        id="call-2", name=TOOL["name"], args={"project_id": "p2"}
                    )),
                ]),
                finish_reason="STOP",
            )],
            usage_metadata=SimpleNamespace(prompt_token_count=23, candidates_token_count=11),
        )
    )
    provider = GeminiProvider(client=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    messages = conversation()
    original = deepcopy(messages)
    result = provider.chat("gemini-test", messages, [TOOL])

    assert result.content == "Ready."
    assert result.tool_calls == [
        ToolCall(id="call-2", name=TOOL["name"], arguments={"project_id": "p2"})
    ]
    assert (result.input_tokens, result.output_tokens) == (23, 11)
    sent = generate.calls[0]
    assert sent["model"] == "gemini-test"
    assert sent["config"]["system_instruction"] == "Keep responses short."
    declaration = sent["config"]["tools"][0]["function_declarations"][0]
    assert declaration["parameters"] == TOOL["input_schema"]
    assert sent["contents"][1]["role"] == "model"
    assert sent["contents"][1]["parts"][1]["function_call"] == {
        "name": TOOL["name"], "args": {"project_id": "p1"}
    }
    assert sent["contents"][2]["parts"][0]["function_response"]["name"] == TOOL["name"]
    assert messages == original


@pytest.mark.parametrize(
    ("module_name", "class_name", "sdk_name", "factory_name", "url_key"),
    [
        ("anthropic", "AnthropicProvider", "anthropic", "Anthropic", "base_url"),
        ("openai", "OpenAIProvider", "openai", "OpenAI", "base_url"),
        ("gemini", "GeminiProvider", "google.genai", "Client", "http_options"),
    ],
)
def test_live_adapter_passes_relay_url_to_lazy_sdk(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    class_name: str,
    sdk_name: str,
    factory_name: str,
    url_key: str,
) -> None:
    module = importlib.import_module(f"avd.llm.providers.{module_name}")
    client = object()
    factory = RecordingCall(client)
    imports: list[str] = []

    def load_sdk(name: str) -> SimpleNamespace:
        imports.append(name)
        assert name == sdk_name
        return SimpleNamespace(**{factory_name: factory})

    monkeypatch.setattr(module.importlib, "import_module", load_sdk)
    adapter = getattr(module, class_name)(api_key="offline-test-key", base_url="https://relay.invalid")
    assert imports == []
    assert adapter._get_client() is client
    assert adapter._get_client() is client
    assert imports == [sdk_name]
    expected_url: Any = {"base_url": "https://relay.invalid"} if url_key == "http_options" else "https://relay.invalid"
    assert factory.calls == [{"api_key": "offline-test-key", url_key: expected_url}]


def test_two_invalid_structured_responses_are_both_billed_and_stop_retrying() -> None:
    from avd.llm.router import LLMRouter, StructuredOutputError

    engine = create_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as session:
        project = ProjectRepo(session).create_project("invalid schema", budget_limit=100)
        provider = FakeProvider([
            ChatResponse(content='{"count":0}', input_tokens=100, output_tokens=20),
            ChatResponse(content="still not JSON", input_tokens=150, output_tokens=30),
            ChatResponse(content='{"count":3}', input_tokens=1, output_tokens=1),
        ])
        settings = Settings(_env_file=None, confirm_threshold=100)
        ledger = CostLedger(session, {
            settings.script_model: ModelPrice(input_per_million=2, output_per_million=4)
        })
        router = LLMRouter(settings, providers={"openai": provider}, ledger=ledger, project=project)
        messages = [{"role": "user", "content": "Count the shots."}]
        original = deepcopy(messages)

        with pytest.raises(StructuredOutputError, match="repair"):
            router.structured("script", messages, ShotCount)

        entries = ledger.list(project.id)
        assert len(entries) == 2
        assert sorted((entry.input_tokens, entry.output_tokens) for entry in entries) == [(100, 20), (150, 30)]
        assert project.spent == pytest.approx((250 * 2 + 50 * 4) / 1_000_000)
        assert all(entry.role == "script" and entry.provider == "openai" for entry in entries)
        assert len(provider.calls) == 2
        assert len(provider.responses) == 1
        assert '{"count":0}' in str(provider.calls[1]["messages"])
        assert messages == original
    engine.dispose()
