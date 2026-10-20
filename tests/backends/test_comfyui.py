from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from avd.backends.comfyui import (
    ComfyUIClient,
    ComfyUIError,
    ComfyUIHTTPError,
    ComfyUINodeError,
    ComfyUIProtocolError,
    ComfyUITimeoutError,
)


def make_client(handler: Any) -> ComfyUIClient:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ComfyUIClient("http://comfy.test", 1.0, client=client, client_id="client-test")


def test_full_upload_submit_wait_download_cycle(tmp_path: Path) -> None:
    source = tmp_path / "frame.png"
    source.write_bytes(b"image input")
    history_calls = 0
    workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "frame.png"}}}

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal history_calls
        if request.url.path == "/upload/image":
            assert request.method == "POST"
            assert b"image input" in request.content
            assert b'name="image"' in request.content
            return httpx.Response(200, json={"name": "uploaded_frame.png"})
        if request.url.path == "/prompt":
            import json

            assert request.method == "POST"
            assert json.loads(request.content) == {"client_id": "client-test", "prompt": workflow}
            return httpx.Response(200, json={"prompt_id": "prompt-1", "node_errors": {}})
        if request.url.path == "/history/prompt-1":
            history_calls += 1
            if history_calls == 1:
                return httpx.Response(200, json={})
            return httpx.Response(
                200,
                json={
                    "prompt-1": {
                        "status": {"completed": True, "status_str": "success"},
                        "outputs": {
                            "7": {
                                "images": [
                                    {
                                        "filename": "output.png",
                                        "subfolder": "run-1",
                                        "type": "output",
                                    }
                                ]
                            },
                            "8": {
                                "audio": [
                                    {"filename": "voice.wav", "subfolder": "", "type": "output"}
                                ]
                            },
                        },
                    }
                },
            )
        if request.url.path == "/view":
            assert request.url.params["type"] == "output"
            if request.url.params["filename"] == "output.png":
                assert request.url.params["subfolder"] == "run-1"
                return httpx.Response(200, content=b"image output")
            assert request.url.params["filename"] == "voice.wav"
            return httpx.Response(200, content=b"audio output")
        raise AssertionError(request.url)

    with make_client(handle) as client:
        assert client.upload_file(source) == "uploaded_frame.png"
        prompt_id = client.submit(workflow)
        history = client.wait(prompt_id, poll_interval=0)
        assert history["status"]["completed"] is True
        output_files = client.download_outputs(prompt_id, tmp_path / "out")
    assert [p.name for p in output_files] == ["output.png", "voice.wav"]
    assert [p.read_bytes() for p in output_files] == [b"image output", b"audio output"]
    assert history_calls == 3


def test_submit_node_error_is_clear() -> None:
    errors = {"5": {"errors": [{"type": "required_input_missing", "message": "Missing model"}]}}
    client = make_client(lambda request: httpx.Response(200, json={"node_errors": errors}))
    with pytest.raises(ComfyUINodeError, match="Missing model") as exc_info:
        client.submit({})
    assert exc_info.value.node_errors == errors


def test_wait_execution_error_is_clear() -> None:
    client = make_client(
        lambda request: httpx.Response(
            200,
            json={
                "p": {
                    "status": {
                        "status_str": "error",
                        "completed": False,
                        "messages": [
                            [
                                "execution_error",
                                {"node_id": "12", "exception_message": "VRAM exhausted"},
                            ]
                        ],
                    },
                    "outputs": {},
                }
            },
        )
    )
    with pytest.raises(ComfyUINodeError, match="VRAM exhausted"):
        client.wait("p", poll_interval=0)


def test_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([0.0, 0.5, 1.0])
    monkeypatch.setattr("avd.backends.comfyui.time.monotonic", lambda: next(times))
    monkeypatch.setattr("avd.backends.comfyui.time.sleep", lambda duration: None)
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ComfyUITimeoutError, match="prompt 'p'.*1s"):
        client.wait("p", poll_interval=0.1, timeout=1.0)


def test_wait_does_not_finish_on_explicit_incomplete_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    monkeypatch.setattr("avd.backends.comfyui.time.sleep", lambda duration: None)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"p": {"status": {"completed": calls > 1}, "outputs": {}}})

    client = make_client(handle)
    client.wait("p", poll_interval=0)
    assert calls == 2


def test_http_and_protocol_errors() -> None:
    client = make_client(lambda request: httpx.Response(500, text="server broken"))
    with pytest.raises(ComfyUIHTTPError, match="HTTP 500.*server broken"):
        client.submit({})
    client = make_client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ComfyUIProtocolError, match="not valid JSON"):
        client.submit({})
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(ComfyUIProtocolError, match="prompt_id"):
        client.submit({})


def test_transport_errors_are_wrapped() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    with pytest.raises(ComfyUIError, match="unreachable"):
        make_client(handle).submit({})


def test_download_keeps_paths_inside_destination_and_avoids_duplicates(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/history/"):
            return httpx.Response(
                200,
                json={
                    "p": {
                        "outputs": {
                            "1": {
                                "images": [
                                    {"filename": "../output.png"},
                                    {"filename": "output.png"},
                                ]
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, content=b"output")

    destination = tmp_path / "download"
    outputs = make_client(handle).download_outputs("p", destination)
    assert [p.name for p in outputs] == ["output.png", "output_2.png"]
    assert all(p.parent == destination for p in outputs)
    assert not (tmp_path / "output.png").exists()


def test_wait_validation_and_upload_missing_file(tmp_path: Path) -> None:
    client = make_client(lambda request: httpx.Response(200, json={}))
    with pytest.raises(FileNotFoundError):
        client.upload_file(tmp_path / "missing.png")
    with pytest.raises(ValueError, match="prompt_id"):
        client.wait("")
    with pytest.raises(ValueError, match="poll_interval"):
        client.wait("p", poll_interval=-1)
    with pytest.raises(ValueError, match="timeout"):
        client.wait("p", timeout=0)
