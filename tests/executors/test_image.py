"""OpenAI image calls are exercised only through an in-memory client."""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avd.backends.image import (
    GeminiImageBackend,
    ImageGenerationRejectedError,
    ImageGenerationUncertainError,
    OpenAIImageBackend,
)
from avd.config import Settings
from avd.jobs.failures import classify_failure
from avd.llm.images import ImageRouter, build_image_backend, still_image_choices

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aDY8AAAAASUVORK5CYII="
)


class FakeImages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _response(self, method: str, arguments: dict) -> SimpleNamespace:
        images = arguments.pop("image", [])
        self.calls.append(
            {"method": method, "references": [image.read() for image in images], **arguments}
        )
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])

    def generate(self, **kwargs: object) -> SimpleNamespace:
        return self._response("generate", kwargs)

    def edit(self, **kwargs: object) -> SimpleNamespace:
        return self._response("edit", kwargs)


def test_images_edit_passes_every_reference_and_configured_model(tmp_path: Path) -> None:
    images = FakeImages()
    adapter = OpenAIImageBackend(client=SimpleNamespace(images=images))
    refs = [tmp_path / "front.png", tmp_path / "side.png"]
    for index, ref in enumerate(refs):
        ref.write_bytes(f"reference-{index}".encode())
    output = adapter.generate(
        prompt="Same character outside",
        reference_images=refs,
        output_path=tmp_path / "result.png",
        model="configured-image-model",
    )
    assert output.read_bytes() == PNG
    assert images.calls == [
        {
            "method": "edit",
            "references": [b"reference-0", b"reference-1"],
            "prompt": "Same character outside",
            "model": "configured-image-model",
            "n": 1,
            "output_format": "png",
        }
    ]
    with pytest.raises(FileExistsError):
        adapter.generate(prompt="again", reference_images=[], output_path=output, model="test")
    assert len(images.calls) == 1


def test_sheet_uses_generation_and_missing_key_is_lazy(tmp_path: Path) -> None:
    adapter = OpenAIImageBackend()
    with pytest.raises(RuntimeError, match="AVD_OPENAI_API_KEY"):
        adapter.generate(
            prompt="sheet", reference_images=[], output_path=tmp_path / "missing.png", model="test"
        )
    images = FakeImages()
    OpenAIImageBackend(client=SimpleNamespace(images=images)).generate(
        prompt="sheet", reference_images=[], output_path=tmp_path / "sheet.png", model="test"
    )
    assert images.calls[0]["method"] == "generate"


@pytest.mark.parametrize("encoded", [None, "not!base64", ""])
def test_invalid_image_response_does_not_create_output(tmp_path: Path, encoded: str | None) -> None:
    client = SimpleNamespace(
        images=SimpleNamespace(
            generate=lambda **_: SimpleNamespace(data=[SimpleNamespace(b64_json=encoded)])
        )
    )
    with pytest.raises(RuntimeError):
        OpenAIImageBackend(client=client).generate(
            prompt="sheet", reference_images=[], output_path=tmp_path / "image.png", model="test"
        )
    assert not (tmp_path / "image.png").exists()


def test_gemini_relay_requests_an_image_through_chat(tmp_path: Path) -> None:
    calls: list[dict] = []
    encoded = base64.b64encode(PNG).decode()

    class Completions:
        def create(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            model_dump=lambda: {
                                "images": [
                                    {"image_url": {"url": f"data:image/png;base64,{encoded}"}}
                                ]
                            }
                        )
                    )
                ]
            )

    adapter = GeminiImageBackend(
        api_key="test-key",
        base_url="https://relay.example/v1",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
    )
    ref = tmp_path / "front.png"
    ref.write_bytes(b"reference")
    output = adapter.generate(
        prompt="Same character",
        reference_images=[ref],
        output_path=tmp_path / "frame.png",
        model="gemini-3.1-flash-image",
    )
    assert output.read_bytes() == PNG
    assert calls[0]["model"] == "gemini-3.1-flash-image"
    assert calls[0]["extra_body"] == {"modalities": ["image", "text"]}
    content = calls[0]["messages"][0]["content"]
    assert content[-1] == {"type": "text", "text": "Same character"}
    assert content[0]["type"] == "image_url"


def test_native_gemini_reads_inline_image_and_references(tmp_path: Path) -> None:
    calls: list[dict] = []

    class Models:
        def generate_content(self, **kwargs: object) -> SimpleNamespace:
            calls.append(kwargs)
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(text="ok", inline_data=None),
                                SimpleNamespace(
                                    text=None,
                                    inline_data=SimpleNamespace(data=PNG, mime_type="image/png"),
                                ),
                            ]
                        )
                    )
                ]
            )

    ref = tmp_path / "front.jpg"
    ref.write_bytes(b"jpeg")
    output = GeminiImageBackend(client=SimpleNamespace(models=Models())).generate(
        prompt="A painted portrait",
        reference_images=[ref],
        output_path=tmp_path / "portrait.png",
        model="gemini-3.1-flash-image",
    )
    assert output.read_bytes() == PNG
    parts = calls[0]["contents"][0]["parts"]
    assert parts[-1] == {"text": "A painted portrait"}
    assert parts[0]["inline_data"]["data"] == b"jpeg"
    assert calls[0]["config"]["response_modalities"] == ["TEXT", "IMAGE"]
    with pytest.raises(RuntimeError, match="AVD_GEMINI_API_KEY"):
        GeminiImageBackend().generate(
            prompt="sheet", reference_images=[], output_path=tmp_path / "missing.png", model="test"
        )


def test_image_router_selects_gemini_when_configured() -> None:
    settings = Settings(
        _env_file=None,
        image_provider="gemini",
        gemini_image_model="gemini-3.1-flash-image",
        gemini_base_url="https://relay.example/v1",
    )
    backend = build_image_backend(settings)
    assert isinstance(backend, GeminiImageBackend)
    assert backend.base_url == "https://relay.example/v1"
    assert settings.image_route() == ("gemini", "gemini-3.1-flash-image")
    openai = build_image_backend(Settings(_env_file=None))
    assert isinstance(openai, OpenAIImageBackend)
    router = ImageRouter(settings, backend=openai)
    assert router.backend is openai


def test_still_image_choices_keep_image_models_and_drop_video() -> None:
    choices = still_image_choices(
        [
            "gpt-image-2",
            "gpt-image-2",
            "gpt-image-2.5-flare",
            "gemini-3.1-flash-image",
            "grok-imagine-image-2.0",
            "grok-imagine-video",
            "grok-4.7",
        ]
    )
    assert [(item["provider"], item["model"]) for item in choices] == [
        ("openai", "gpt-image-2"),
        ("openai", "gpt-image-2.5-flare"),
        ("gemini", "gemini-3.1-flash-image"),
        ("grok", "grok-imagine-image-2.0"),
    ]


def test_image_router_selects_grok_imagine(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        image_provider="grok",
        grok_image_model="grok-imagine-image-2.0",
        grok_base_url="https://api.x.ai/v1",
        grok_api_key="test-key",
    )
    backend = build_image_backend(settings)
    assert isinstance(backend, OpenAIImageBackend)
    assert backend.base_url == "https://api.x.ai/v1"
    assert backend.api_key == "test-key"
    assert backend.missing_key == "AVD_GROK_API_KEY"
    assert settings.image_route() == ("grok", "grok-imagine-image-2.0")
    with pytest.raises(RuntimeError, match="AVD_GROK_API_KEY"):
        build_image_backend(Settings(_env_file=None, image_provider="grok")).generate(
            prompt="sheet",
            reference_images=[],
            output_path=tmp_path / "missing-grok.png",
            model="grok-imagine-image-2.0",
        )


def test_url_only_image_response_downloads_without_api_credentials(tmp_path: Path) -> None:
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    sdk = SimpleNamespace(
        images=SimpleNamespace(
            generate=lambda **kwargs: SimpleNamespace(
                data=[
                    SimpleNamespace(
                        b64_json=None, url="https://media.example/image.png?signature=test"
                    )
                ]
            )
        )
    )
    with httpx.Client(transport=httpx.MockTransport(serve)) as download:
        output = OpenAIImageBackend(client=sdk, download_client=download).generate(
            prompt="scene", reference_images=[], output_path=tmp_path / "out.png", model="test"
        )
    assert output.read_bytes() == PNG
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize("content", ["Nope", "SGVsbG8=", "data:image/png;base64,SGVsbG8="])
def test_gemini_ordinary_text_is_not_written_as_image(tmp_path: Path, content: str) -> None:
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(model_dump=lambda: {"content": content})
                        )
                    ]
                )
            )
        )
    )
    with pytest.raises(RuntimeError, match="did not include an image"):
        GeminiImageBackend(base_url="https://relay.example/v1", client=sdk).generate(
            prompt="scene", reference_images=[], output_path=tmp_path / "out.png", model="test"
        )
    assert not (tmp_path / "out.png").exists()


def test_url_download_with_nonimage_body_does_not_publish(tmp_path: Path) -> None:
    sdk = SimpleNamespace(
        images=SimpleNamespace(
            generate=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(b64_json=None, url="https://media.example/error")]
            )
        )
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="error"))
    ) as c:
        with pytest.raises(ImageGenerationUncertainError) as caught:
            OpenAIImageBackend(client=sdk, download_client=c).generate(
                prompt="scene", reference_images=[], output_path=tmp_path / "out.png", model="test"
            )
    assert "invalid image content" in str(caught.value.__cause__)
    assert not (tmp_path / "out.png").exists()


class ImageStream:
    def __init__(self, events, *, on_iterate=None):
        self.events = events
        self.closed = False
        self.on_iterate = on_iterate

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def __iter__(self):
        if self.on_iterate:
            self.on_iterate()
        for event in self.events:
            if isinstance(event, Exception):
                raise event
            yield event


def test_gpt_image_stream_publishes_only_the_final_image(tmp_path: Path) -> None:
    calls = []
    stream = ImageStream(
        [
            SimpleNamespace(type="keepalive"),
            SimpleNamespace(
                type="image_generation.partial_image",
                b64_json=base64.b64encode(b"preview").decode(),
            ),
            SimpleNamespace(
                type="image_generation.completed", b64_json=base64.b64encode(PNG).decode()
            ),
            ConnectionError("a completed image must not wait for an additional stream event"),
        ],
    )

    def request(**kwargs):
        calls.append(kwargs)
        return stream

    sdk = SimpleNamespace(images=SimpleNamespace(generate=request))
    output = OpenAIImageBackend(client=sdk).generate(
        prompt="Original character sheet",
        reference_images=[],
        output_path=tmp_path / "image.png",
        model="gpt-image-2.5-sunburst",
    )
    assert output.read_bytes() == PNG
    assert calls == [
        {
            "prompt": "Original character sheet",
            "model": "gpt-image-2.5-sunburst",
            "n": 1,
            "output_format": "png",
            "stream": True,
            "partial_images": 1,
        }
    ]
    assert stream.closed


@pytest.mark.parametrize("stream_images", [False, True])
def test_reference_edit_uses_one_complete_response_and_keeps_uploads_open(
    tmp_path: Path, stream_images: bool
) -> None:
    calls = []
    handles = []
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG)

    def edit(**kwargs):
        handles.extend(kwargs.pop("image"))
        assert [handle.read() for handle in handles] == [PNG]
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())])

    # No generate method: the adapter must not submit a second request as a fallback.
    sdk = SimpleNamespace(images=SimpleNamespace(edit=edit))
    output = OpenAIImageBackend(client=sdk, stream_images=stream_images).generate(
        prompt="Replace the scene while preserving the performer",
        reference_images=[reference],
        output_path=tmp_path / "image.png",
        model="gpt-image-2.5-sunburst",
    )
    assert output.read_bytes() == PNG
    assert calls == [
        {
            "prompt": "Replace the scene while preserving the performer",
            "model": "gpt-image-2.5-sunburst",
            "n": 1,
            "output_format": "png",
        }
    ]
    assert handles and all(handle.closed for handle in handles)


@pytest.mark.parametrize(
    "ending", ["partial_only", "error", "interrupt", "invalid_final", "wrong_kind"]
)
def test_image_stream_failures_do_not_publish_a_partial_image_or_resend(
    tmp_path: Path, ending: str
):
    events = [
        SimpleNamespace(
            type="image_generation.partial_image", b64_json=base64.b64encode(PNG).decode()
        )
    ]
    if ending == "error":
        events.append(SimpleNamespace(type="error"))
    elif ending == "interrupt":
        events.append(ConnectionError("relay closed the stream"))
    elif ending == "invalid_final":
        events.append(SimpleNamespace(type="image_generation.completed", b64_json="not!base64"))
    elif ending == "wrong_kind":
        events.append(
            SimpleNamespace(type="image_edit.completed", b64_json=base64.b64encode(PNG).decode())
        )
    stream = ImageStream(events)
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return stream

    sdk = SimpleNamespace(images=SimpleNamespace(generate=generate))
    expected_error = (
        ImageGenerationUncertainError
        if ending in {"partial_only", "interrupt", "wrong_kind"}
        else RuntimeError
    )
    with pytest.raises(expected_error):
        OpenAIImageBackend(client=sdk).generate(
            prompt="Original character",
            reference_images=[],
            output_path=tmp_path / "out.png",
            model="gpt-image-2.5-sunburst",
        )
    assert not (tmp_path / "out.png").exists()
    assert stream.closed and len(calls) == 1


@pytest.mark.parametrize("with_reference", [False, True])
@pytest.mark.parametrize(
    "status,body,code,retryable",
    [
        (524, {}, "image_outcome_unknown", False),
        (401, {}, "authentication_failed", False),
        (400, {}, "invalid_request", False),
        (429, {"error": {"code": "rate_limit_exceeded"}}, "rate_limited", True),
        (429, {"error": {"code": "insufficient_quota"}}, "quota_exhausted", False),
    ],
)
def test_image_submission_stops_only_uncertain_results_without_hiding_rejections(
    tmp_path: Path, with_reference: bool, status: int, body: dict, code: str, retryable: bool
) -> None:
    calls = []

    class GatewayError(RuntimeError):
        def __init__(self):
            self.status_code = status
            self.body = body
            super().__init__("provider response contains private details")

    rejection = GatewayError()

    def request(**kwargs):
        calls.append(kwargs)
        raise rejection

    sdk = SimpleNamespace(images=SimpleNamespace(generate=request, edit=request))
    refs = [tmp_path / "reference.png"] if with_reference else []
    for reference in refs:
        reference.write_bytes(PNG)
    expected_error = ImageGenerationUncertainError if status == 524 else GatewayError
    with pytest.raises(expected_error) as caught:
        OpenAIImageBackend(client=sdk).generate(
            prompt="Original character",
            reference_images=refs,
            output_path=tmp_path / "out.png",
            model="gpt-image-2.5-sunburst",
        )
    if status != 524:
        assert caught.value is rejection
    else:
        assert caught.value.__cause__ is rejection
    failure = classify_failure(caught.value)
    assert failure["code"] == code
    assert failure["retryable"] is retryable
    assert failure["http_status"] == status
    assert "private details" not in failure["message"]
    assert len(calls) == 1 and not (tmp_path / "out.png").exists()


@pytest.mark.parametrize("with_reference", [False, True])
@pytest.mark.parametrize(
    "envelope,exception,code",
    [
        (
            {"code": "moderation_blocked", "type": "image_generation_user_error"},
            ImageGenerationRejectedError,
            "moderation_blocked",
        ),
        (
            {"code": "unknown_provider_failure"},
            ImageGenerationUncertainError,
            "image_outcome_unknown",
        ),
        ({"code": "rate_limit_exceeded"}, ImageGenerationUncertainError, "image_outcome_unknown"),
        ({"code": "insufficient_quota"}, ImageGenerationUncertainError, "image_outcome_unknown"),
        ({"message": "moderation_blocked"}, ImageGenerationUncertainError, "image_outcome_unknown"),
        ({}, ImageGenerationUncertainError, "image_outcome_unknown"),
    ],
)
def test_http_200_image_error_envelope_is_not_published_or_automatically_resubmitted(
    tmp_path: Path, with_reference: bool, envelope: dict, exception: type[Exception], code: str
) -> None:
    # Exercise the installed SDK's loose HTTP-200 parsing, not a stricter Pydantic
    # model_validate call, which would reject the gateway's missing created field.
    openai = pytest.importorskip("openai")
    calls = []
    error = {**envelope, "message": "private-provider-message sk-offline-secret"}
    if envelope == {"message": "moderation_blocked"}:
        error["message"] += " moderation_blocked"

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"error": error})

    references = [tmp_path / "reference.png"] if with_reference else []
    for path in references:
        path.write_bytes(PNG)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        sdk = openai.OpenAI(
            api_key="offline-test",
            base_url="http://offline.test/v1",
            http_client=client,
            max_retries=0,
        )
        adapter = OpenAIImageBackend(client=sdk, stream_images=False)
        with pytest.raises(exception) as caught:
            adapter.generate(
                prompt="Original character",
                reference_images=references,
                output_path=tmp_path / "out.png",
                model="gpt-image-test",
            )
    failure = classify_failure(caught.value)
    assert failure["code"] == code
    assert failure["retryable"] is False and failure["http_status"] is None
    assert "private-provider-message" not in str(caught.value)
    assert "sk-offline-secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert len(calls) == 1 and not (tmp_path / "out.png").exists()


def test_explicit_error_envelope_takes_precedence_over_conflicting_image_data(
    tmp_path: Path,
) -> None:
    sdk = SimpleNamespace(
        images=SimpleNamespace(
            generate=lambda **_: SimpleNamespace(
                error={"code": "moderation_blocked", "message": "private provider detail"},
                data=[SimpleNamespace(b64_json=base64.b64encode(PNG).decode())],
            )
        )
    )
    with pytest.raises(ImageGenerationRejectedError):
        OpenAIImageBackend(client=sdk, stream_images=False).generate(
            prompt="Original character",
            reference_images=[],
            output_path=tmp_path / "out.png",
            model="test",
        )
    assert not (tmp_path / "out.png").exists()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("stream read timed out"),
        httpx.RemoteProtocolError("relay disconnected"),
        httpx.ReadError("stream read failed"),
    ],
)
def test_an_accepted_image_stream_transport_failure_requires_manual_retry(
    tmp_path: Path, error: Exception
) -> None:
    stream = ImageStream([SimpleNamespace(type="keepalive"), error])
    sdk = SimpleNamespace(images=SimpleNamespace(generate=lambda **kwargs: stream))
    with pytest.raises(ImageGenerationUncertainError) as caught:
        OpenAIImageBackend(client=sdk).generate(
            prompt="Original character",
            reference_images=[],
            output_path=tmp_path / "out.png",
            model="gpt-image-2.5-sunburst",
        )
    assert caught.value.__cause__ is error
    assert stream.closed and not (tmp_path / "out.png").exists()
    assert classify_failure(caught.value)["retryable"] is False


@pytest.mark.parametrize("with_reference", [False, True])
@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_image_response_transport_failure_does_not_resubmit(
    tmp_path: Path, with_reference: bool, error_type: type[Exception]
) -> None:
    calls = []
    handles = []
    error = error_type("response interrupted after submission")

    def request(**kwargs):
        calls.append(kwargs)
        handles.extend(kwargs.get("image", []))
        raise error

    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG)
    sdk = SimpleNamespace(images=SimpleNamespace(generate=request, edit=request))
    with pytest.raises(ImageGenerationUncertainError) as caught:
        OpenAIImageBackend(client=sdk).generate(
            prompt="Stage photograph",
            reference_images=[reference] if with_reference else [],
            output_path=tmp_path / "out.png",
            model="gpt-image-2.5-sunburst",
        )
    assert caught.value.__cause__ is error
    assert len(calls) == 1 and all(handle.closed for handle in handles)
    assert not (tmp_path / "out.png").exists()
    assert classify_failure(caught.value)["retryable"] is False


def test_image_sdk_disables_hidden_retries(monkeypatch):
    calls = []
    sdk = object()

    def client(**kwargs):
        calls.append(kwargs)
        return sdk

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    adapter = OpenAIImageBackend(api_key="offline-test-key", base_url="https://relay.invalid/v1")
    assert adapter._get_client() is sdk
    assert adapter._get_client() is sdk
    assert calls == [
        {"api_key": "offline-test-key", "base_url": "https://relay.invalid/v1", "max_retries": 0}
    ]
