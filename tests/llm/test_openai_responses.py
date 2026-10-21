"""Offline Responses contracts for reasoning-model director tool loops."""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from types import SimpleNamespace

import pytest

from avd.config import ModelPrice, Settings
from avd.jobs.failures import classify_failure
from avd.llm.ledger import CostLedger
from avd.llm.providers.base import ProviderConfigurationError, ProviderResponseError
from avd.llm.providers.openai import OpenAIProvider
from avd.llm.router import LLMRouter
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.repo import ProjectRepo

TOOL = {
    "name": "get_project_status",
    "description": "Read project status.",
    "input_schema": {"type": "object", "properties": {"detail": {"type": "boolean"}}},
}


def reply(*output, status="completed"):
    return SimpleNamespace(
        output=list(output),
        status=status,
        usage=SimpleNamespace(input_tokens=23, output_tokens=11),
    )


def text_item(text="Ready."):
    return {
        "type": "message",
        "id": "msg_final",
        "role": "assistant",
        "status": "completed",
        "phase": "final_answer",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


class ScriptedStream:
    def __init__(self, events, final=None):
        self.events = iter(events)
        self.final = final
        self.closed = False
        self.final_reads = 0
        self.exhausted = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event
        self.exhausted = True

    def get_final_response(self):
        self.final_reads += 1
        assert self.exhausted
        if self.final is None or self.final.status != "completed":
            raise RuntimeError("No completed response")
        return self.final


def client_for(*responses):
    pending = list(responses)
    calls = []

    streams = []

    def stream(**kwargs):
        calls.append(deepcopy(kwargs))
        response = pending.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, ScriptedStream):
            scripted = response
        else:
            scripted = ScriptedStream(
                [
                    SimpleNamespace(
                        type="response.created",
                        response=SimpleNamespace(status="in_progress", output=[], usage=None),
                    ),
                    SimpleNamespace(type=f"response.{response.status}", response=response),
                ],
                final=response,
            )
        streams.append(scripted)
        return scripted

    return SimpleNamespace(responses=SimpleNamespace(stream=stream, streams=streams)), calls


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6-astra-2026-10-01", "gpt-6.1-sol"])
def test_reasoning_families_use_responses_for_plain_connection_probes(model):
    sdk, calls = client_for(reply(text_item()))
    result = OpenAIProvider(client=sdk, max_output_tokens=32).chat(
        model, [{"role": "user", "content": "Reply OK."}]
    )
    assert result.content == "Ready."
    assert (result.input_tokens, result.output_tokens, result.stop_reason) == (23, 11, "completed")
    assert calls == [
        {
            "model": model,
            "input": [{"role": "user", "content": "Reply OK."}],
            "max_output_tokens": 32,
            "store": False,
            "include": ["reasoning.encrypted_content"],
        }
    ]
    assert sdk.responses.streams[0].closed
    assert sdk.responses.streams[0].final_reads == 0


def test_responses_round_trip_preserves_reasoning_phase_parallel_calls_and_call_ids():
    reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
    commentary = text_item("Checking project state.")
    commentary["phase"] = "commentary"
    function_calls = [
        {
            "type": "function_call",
            "id": f"fc_{index}",
            "call_id": f"call_{index}",
            "name": TOOL["name"],
            "arguments": json.dumps({"detail": bool(index)}),
            "status": "completed",
        }
        for index in range(2)
    ]

    # A real SDK item is a model with model_dump; accept model objects as well as dicts.
    class SDKReasoning:
        def model_dump(self, **kwargs):
            assert kwargs == {"mode": "json", "exclude_none": True}
            return deepcopy(reasoning)

    sdk, calls = client_for(
        reply(SDKReasoning(), commentary, *function_calls), reply(text_item("All checked."))
    )
    provider = OpenAIProvider(client=sdk)
    messages = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "Check."}]
    original = deepcopy(messages)
    first = provider.chat("gpt-6-astra", messages, [TOOL])
    assert [call.id for call in first.tool_calls] == ["call_0", "call_1"]
    assert first.content == "Checking project state."
    assert first.provider_blocks["openai_responses"] == [reasoning, commentary, *function_calls]
    assert messages == original
    # Match persistence through the database's JSON column.
    messages.append(json.loads(json.dumps(first.as_message())))
    messages.extend(
        {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": "checked"}
        for call in first.tool_calls
    )
    second = provider.chat("gpt-6-astra", messages, [TOOL])
    assert second.content == "All checked." and not second.tool_calls
    sent = calls[1]["input"]
    assert sent[:2] == original
    assert sent[2:6] == [reasoning, commentary, *function_calls]
    assert sent[6:] == [
        {"type": "function_call_output", "call_id": f"call_{index}", "output": "checked"}
        for index in range(2)
    ]
    assert len([item for item in sent if item.get("type") == "function_call"]) == 2
    assert calls[0]["tools"] == [
        {
            "type": "function",
            "name": TOOL["name"],
            "description": TOOL["description"],
            "parameters": TOOL["input_schema"],
            "strict": False,
        }
    ]
    assert "previous_response_id" not in calls[1]


def test_responses_accepts_canonical_tool_history_from_a_previous_chat_model():
    sdk, calls = client_for(reply(text_item()))
    history = [
        {
            "role": "assistant",
            "content": "Checking.",
            "provider_blocks": {"anthropic": [{"type": "thinking", "thinking": "other provider"}]},
            "tool_calls": [{"id": "legacy-call", "name": TOOL["name"], "arguments": {}}],
        },
        {"role": "tool", "tool_call_id": "legacy-call", "content": "done"},
    ]
    OpenAIProvider(client=sdk).chat("gpt-6.1-sol", history, [TOOL])
    assert calls[0]["input"] == [
        {"role": "assistant", "content": "Checking."},
        {
            "type": "function_call",
            "call_id": "legacy-call",
            "name": TOOL["name"],
            "arguments": "{}",
        },
        {"type": "function_call_output", "call_id": "legacy-call", "output": "done"},
    ]


def test_responses_converts_local_image_inputs_without_leaking_history_metadata(tmp_path):
    image = tmp_path / "reference.png"
    image.write_bytes(b"offline image bytes")
    sdk, calls = client_for(reply(text_item()))
    history = [
        {
            "role": "user",
            "event": True,
            "event_id": "notice",
            "attachments": [],
            "content": [
                {"type": "text", "text": "Describe."},
                {"type": "image", "path": str(image)},
            ],
        }
    ]
    original = deepcopy(history)
    OpenAIProvider(client=sdk).chat("gpt-6-astra", history)
    sent = calls[0]["input"][0]
    assert set(sent) == {"role", "content"}
    assert sent["content"][0] == {"type": "input_text", "text": "Describe."}
    assert sent["content"][1]["type"] == "input_image"
    assert sent["content"][1]["image_url"].startswith("data:image/png;base64,")
    assert history == original


def test_responses_rejects_video_before_any_request():
    sdk, calls = client_for(reply())
    with pytest.raises(ProviderConfigurationError, match="video"):
        OpenAIProvider(client=sdk).chat(
            "gpt-6-astra",
            [
                {
                    "role": "user",
                    "content": [{"type": "video", "path": "source.mp4"}],
                }
            ],
        )
    assert calls == []


@pytest.mark.parametrize("label,model", [("Gemini", "gpt-6-astra"), ("OpenAI", "gpt-other")])
def test_other_provider_labels_and_chat_models_keep_chat_completions(label, model):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="chat", tool_calls=[]), finish_reason="stop"
                )
            ],
            usage=None,
        )

    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = OpenAIProvider(client=sdk, label=label).chat(
        model, [{"role": "user", "content": "Hi"}]
    )
    assert result.content == "chat"
    assert len(calls) == 1 and "messages" in calls[0]


@pytest.mark.parametrize("invalid", ["arguments", "incomplete", "failed"])
def test_completed_but_unusable_responses_preserve_usage_in_the_ledger(invalid):
    output = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": TOOL["name"],
        "arguments": "invalid JSON",
    }
    response = reply(output) if invalid == "arguments" else reply(text_item(), status=invalid)
    sdk, calls = client_for(response)
    database = create_engine("sqlite+pysqlite:///:memory:")
    init_db(database)
    try:
        with create_session_factory(database)() as session:
            project = ProjectRepo(session).create_project(
                "offline Responses billing", budget_limit=100
            )
            settings = Settings(
                _env_file=None,
                confirm_threshold=100,
                llm_routes={"director": {"provider": "openai", "model": "gpt-6-astra"}},
            )
            ledger = CostLedger(
                session, {"gpt-6-astra": ModelPrice(input_per_million=2, output_per_million=4)}
            )
            router = LLMRouter(
                settings,
                project=project,
                ledger=ledger,
                providers={"openai": OpenAIProvider(client=sdk)},
            )
            with pytest.raises(ProviderResponseError) as caught:
                router.chat("director", [{"role": "user", "content": "Check"}], [TOOL])
            assert (caught.value.response.input_tokens, caught.value.response.output_tokens) == (
                23,
                11,
            )
            [entry] = ledger.list(project.id)
            assert (entry.input_tokens, entry.output_tokens) == (23, 11)
            assert project.spent == pytest.approx((23 * 2 + 11 * 4) / 1_000_000)
            assert len(calls) == 1
    finally:
        database.dispose()


def test_responses_failure_is_not_retried_through_a_different_endpoint():
    sdk, calls = client_for(RuntimeError("relay response unavailable"))
    with pytest.raises(RuntimeError, match="relay response unavailable"):
        OpenAIProvider(client=sdk).chat("gpt-6-astra", [{"role": "user", "content": "Hi"}])
    assert len(calls) == 1


def test_stream_waits_for_complete_arguments_instead_of_parsing_partial_chunks():
    final = reply(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": TOOL["name"],
            "arguments": '{"detail":true}',
        }
    )
    scripted = ScriptedStream(
        [
            SimpleNamespace(
                type="response.created",
                response=SimpleNamespace(status="in_progress", output=[], usage=None),
            ),
            SimpleNamespace(type="response.function_call_arguments.delta", delta='{"detail":'),
            SimpleNamespace(type="response.function_call_arguments.delta", delta="true}"),
            SimpleNamespace(type="response.completed", response=final),
        ],
        final=final,
    )
    sdk, calls = client_for(scripted)
    result = OpenAIProvider(client=sdk).chat(
        "gpt-6-astra", [{"role": "user", "content": "Check"}], [TOOL]
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {"detail": True}
    assert result.output_tokens == 11
    assert scripted.final_reads == 0 and scripted.closed
    assert len(calls) == 1


@pytest.mark.parametrize("ending", ["incomplete", "failed", "error", "eof", "interrupt"])
def test_stream_failure_never_returns_partial_tools_and_preserves_available_usage(ending):
    partial = reply(
        {
            "type": "function_call",
            "id": "fc_partial",
            "call_id": "partial",
            "name": TOOL["name"],
            "arguments": '{"detail":',
        },
        status="in_progress",
    )
    events = [SimpleNamespace(type="response.created", response=partial)]
    if ending in {"incomplete", "failed"}:
        events.append(SimpleNamespace(type=f"response.{ending}", response=reply(status=ending)))
    elif ending == "error":
        events.append(SimpleNamespace(type="error", code="relay_error", message="stream stopped"))
    elif ending == "interrupt":
        events.append(ConnectionError("stream disconnected"))
    scripted = ScriptedStream(events)
    sdk, calls = client_for(scripted)
    with pytest.raises(ProviderResponseError) as caught:
        OpenAIProvider(client=sdk).chat(
            "gpt-6-astra", [{"role": "user", "content": "Check"}], [TOOL]
        )
    response = caught.value.response
    assert (response.input_tokens, response.output_tokens) == (23, 11)
    assert not response.tool_calls
    assert scripted.final_reads == 0 and scripted.closed
    assert len(calls) == 1


def test_empty_stream_cannot_be_treated_as_a_successful_empty_reply():
    scripted = ScriptedStream([])
    sdk, calls = client_for(scripted)
    with pytest.raises(ProviderResponseError, match="without completion") as caught:
        OpenAIProvider(client=sdk).chat("gpt-6-astra", [{"role": "user", "content": "Check"}])
    assert caught.value.response.input_tokens == 0
    assert scripted.closed and len(calls) == 1


@pytest.mark.parametrize("channel", ["error", "failed"])
@pytest.mark.parametrize(
    "upstream_code,message,code,retryable,retry_at",
    [
        (
            "quota_exhausted",
            'Individual quota reached; quotaResetTimeStamp="2026-10-21T09:44:09Z"',
            "quota_exhausted",
            False,
            "2026-10-21T09:44:09Z",
        ),
        ("rate_limit_exceeded", "Too many requests", "rate_limited", True, None),
        ("invalid_prompt", "Invalid prompt", "invalid_request", False, None),
        ("invalid_api_key", "Access denied", "authentication_failed", False, None),
    ],
)
def test_sse_failures_retain_safe_classification_after_worker_traceback_serialization(
    channel, upstream_code, message, code, retryable, retry_at
):
    upstream = SimpleNamespace(
        code=upstream_code, message=message + " <html>sk-offline-hidden-secret</html>"
    )
    if channel == "error":
        event = SimpleNamespace(type="error", **vars(upstream))
    else:
        failed = reply(status="failed")
        failed.error = upstream
        event = SimpleNamespace(type="response.failed", response=failed)
    scripted = ScriptedStream([event])
    sdk, calls = client_for(scripted)
    with pytest.raises(ProviderResponseError) as caught:
        OpenAIProvider(client=sdk).chat("gpt-6-astra", [{"role": "user", "content": "Check"}])
    failure = classify_failure(caught.value)
    serialized = "".join(traceback.format_exception(caught.value))
    isolated = classify_failure(RuntimeError("Job process failed:\n" + serialized))
    assert failure == isolated
    assert (failure["code"], failure["retryable"], failure["retry_at"]) == (
        code,
        retryable,
        retry_at,
    )
    assert "hidden" not in str(caught.value) and "<html>" not in str(caught.value)
    assert not caught.value.response.tool_calls
    assert scripted.closed and len(calls) == 1


@pytest.mark.parametrize(
    "error,code",
    [
        (TimeoutError("sk-offline-hidden-timeout"), "timeout"),
        (ConnectionError("sk-offline-hidden-connection"), "connection_failed"),
    ],
)
def test_transport_failure_after_stream_start_retains_classification(error, code):
    scripted = ScriptedStream(
        [SimpleNamespace(type="response.created", response=reply(status="in_progress")), error]
    )
    sdk, calls = client_for(scripted)
    with pytest.raises(ProviderResponseError) as caught:
        OpenAIProvider(client=sdk).chat("gpt-6-astra", [{"role": "user", "content": "Check"}])
    assert classify_failure(caught.value)["code"] == code
    assert "hidden" not in str(caught.value)
    assert (caught.value.response.input_tokens, caught.value.response.output_tokens) == (23, 11)
    assert scripted.closed and len(calls) == 1


def test_terminal_completion_survives_later_disconnect_and_records_usage_once():
    final = reply(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": TOOL["name"],
            "arguments": '{"detail":true}',
        }
    )
    scripted = ScriptedStream(
        [
            SimpleNamespace(type="response.completed", response=final),
            ConnectionError("disconnect after the complete response"),
        ],
        final=final,
    )
    sdk, calls = client_for(scripted)
    database = create_engine("sqlite+pysqlite:///:memory:")
    init_db(database)
    try:
        with create_session_factory(database)() as session:
            project = ProjectRepo(session).create_project("Terminal response", budget_limit=100)
            settings = Settings(
                _env_file=None,
                confirm_threshold=100,
                llm_routes={"director": {"provider": "openai", "model": "gpt-6-astra"}},
            )
            ledger = CostLedger(
                session, {"gpt-6-astra": ModelPrice(input_per_million=2, output_per_million=4)}
            )
            router = LLMRouter(
                settings,
                project=project,
                ledger=ledger,
                providers={"openai": OpenAIProvider(client=sdk)},
            )
            result = router.chat("director", [{"role": "user", "content": "Check"}], [TOOL])
            assert [call.id for call in result.tool_calls] == ["call_1"]
            assert result.tool_calls[0].arguments == {"detail": True}
            [entry] = ledger.list(project.id)
            assert (entry.input_tokens, entry.output_tokens) == (23, 11)
            assert project.spent == pytest.approx((23 * 2 + 11 * 4) / 1_000_000)
            assert len(calls) == 1
            assert scripted.closed and scripted.final_reads == 0
            assert not scripted.exhausted
    finally:
        database.dispose()
