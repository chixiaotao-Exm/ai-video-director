"""Lazy image adapters with injectable clients for offline tests."""

from __future__ import annotations

import base64
import binascii
import mimetypes
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from avd.llm.router import gemini_uses_openai_compat


class ImageBackend(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        reference_images: Sequence[Path],
        output_path: Path,
        model: str,
    ) -> Path: ...


class ImageGenerationUncertainError(RuntimeError):
    """A paid image request may still be executing; never resubmit it automatically."""

    def __init__(self, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(
            "Image generation outcome is unknown"
            + status
            + "; inspect the provider before manually retrying"
        )


class ImageGenerationRejectedError(RuntimeError):
    """An explicit image safety rejection, without the private provider payload."""

    def __init__(self) -> None:
        super().__init__("Images API rejected the request (moderation_blocked)")


class OpenAIImageBackend:
    """Use image edits for references and image generation for a new sheet.

    Neither importing this module nor constructing the adapter imports the SDK
    or contacts a service. Cost accounting belongs to the rebuild engine.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: Any = None,
        missing_key: str = "AVD_OPENAI_API_KEY",
        download_client: httpx.Client | None = None,
        response_format: str | None = None,
        stream_images: bool = True,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = client
        self.missing_key = missing_key
        self.download_client = download_client
        self.response_format = response_format
        self.stream_images = stream_images

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(f"Set {self.missing_key} before generating images")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install avd[llm] to use the OpenAI image backend") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url, max_retries=0)
        return self._client

    def generate(
        self,
        *,
        prompt: str,
        reference_images: Sequence[Path],
        output_path: Path,
        model: str,
    ) -> Path:
        if not prompt.strip() or not model.strip():
            raise ValueError("Image generation requires a prompt and configured model")
        output_path = Path(output_path)
        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite image output: {output_path}")
        references = [Path(path) for path in reference_images]
        for path in references:
            if not path.is_file():
                raise FileNotFoundError(f"Reference image does not exist: {path}")
        client = self._get_client()
        arguments: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        if self.response_format:
            arguments["response_format"] = self.response_format
        else:
            arguments["output_format"] = "png"
        # Compatible gateways may return no parseable edit events. Choose the
        # complete response before submission, never retry an edit as a fallback.
        streaming = self.stream_images and not references and model.startswith("gpt-image")
        if streaming:
            arguments.update(stream=True, partial_images=1)
        if references:
            with ExitStack() as stack:
                handles = [stack.enter_context(path.open("rb")) for path in references]
                response = self._request(client.images.edit, image=handles, **arguments)
                return self._publish(response, output_path, streaming=False)
        else:
            response = self._request(client.images.generate, **arguments)
            return self._publish(response, output_path, streaming=streaming)

    def _publish(self, response: Any, output_path: Path, *, streaming: bool) -> Path:
        # Once accepted, retrieving or validating the result must never trigger
        # another paid generation. Preserve the cause for manual diagnosis.
        try:
            if streaming:
                return _write_png(
                    output_path, _completed_image(response, "image_generation"), source="Images API"
                )
            # A gateway keepalive may commit HTTP 200 before an upstream error.
            # SDK models retain that error envelope as an extra field. Never
            # publish accompanying data or expose arbitrary provider messages.
            rejection = getattr(response, "error", None)
            if rejection is not None:
                if isinstance(rejection, dict) and rejection.get("code") == "moderation_blocked":
                    raise ImageGenerationRejectedError()
                raise ImageGenerationUncertainError()
            data = getattr(response, "data", None)
            encoded = getattr(data[0], "b64_json", None) if data else None
            url = getattr(data[0], "url", None) if data else None
            if not encoded and isinstance(url, str) and url:
                return _write_png(output_path, self._download(url), source="Images API")
            if not isinstance(encoded, str) or not encoded:
                raise RuntimeError("OpenAI Images returned no base64 image")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("OpenAI Images returned invalid base64 image data") from exc
            return _write_png(output_path, content, source="Images API")
        except (ImageGenerationUncertainError, ImageGenerationRejectedError):
            raise
        except Exception as exc:
            raise ImageGenerationUncertainError() from exc

    @staticmethod
    def _request(method: Callable[..., Any], **arguments: Any) -> Any:
        try:
            return method(**arguments)
        except Exception as exc:
            if getattr(exc, "status_code", None) == 524:
                raise ImageGenerationUncertainError(status_code=524) from exc
            if _is_transport_failure(exc):
                raise ImageGenerationUncertainError() from exc
            raise

    def _download(self, url: str) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise RuntimeError("Images API returned an invalid image URL")
        # Download without API credentials. Signed URLs belong to the media host.
        with ExitStack() as stack:
            client = self.download_client or stack.enter_context(
                httpx.Client(timeout=60, follow_redirects=False)
            )
            with client.stream("GET", url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 40 * 1024 * 1024:
                        raise RuntimeError("Images API image exceeds 40 MB")
                    chunks.append(chunk)
                return b"".join(chunks)


def _is_transport_failure(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, TimeoutError, OSError, httpx.TransportError)) or type(
        exc
    ).__name__ in {"APIConnectionError", "APITimeoutError"}


def _completed_image(stream: Any, kind: str) -> bytes:
    """Only publish a final image; partial previews and keepalives are not success."""
    encoded = None
    try:
        with stream:
            for event in stream:
                event_type = getattr(event, "type", None)
                if event_type == f"{kind}.completed":
                    encoded = getattr(event, "b64_json", None)
                    break
                if event_type in {"error", f"{kind}.failed", f"{kind}.incomplete"}:
                    raise RuntimeError(
                        "Images API stream failed before a complete image was available"
                    )
    except Exception as exc:
        if _is_transport_failure(exc):
            raise ImageGenerationUncertainError() from exc
        raise
    if not isinstance(encoded, str) or not encoded:
        raise ImageGenerationUncertainError()
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Images API returned invalid base64 image data") from exc


def _is_image(content: bytes) -> bool:
    """Reject ordinary text/base64 blobs before publishing an image artifact."""
    return (
        (
            len(content) >= 33
            and content.startswith(b"\x89PNG\r\n\x1a\n")
            and content[12:16] == b"IHDR"
        )
        or (
            len(content) > 4
            and content.startswith(b"\xff\xd8\xff")
            and content.endswith(b"\xff\xd9")
        )
        or (len(content) >= 20 and content.startswith(b"RIFF") and content[8:12] == b"WEBP")
    )


def _write_png(output_path: Path, content: bytes, *, source: str) -> Path:
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite image output: {output_path}")
    if not content:
        raise RuntimeError(f"{source} returned an empty image")
    if not _is_image(content):
        raise RuntimeError(f"{source} returned invalid image content")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("xb") as handle:
        handle.write(content)
    return output_path


class GeminiImageBackend:
    """Generate stills with Gemini.

    A base URL ending in ``/v1`` or ``/openai`` is an OpenAI-compatible relay.
    Those relays reject Gemini image models on the Images API, so the call uses
    chat completions and asks for an image. An empty address uses Google's
    native image response.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: Any = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = client

    def generate(
        self,
        *,
        prompt: str,
        reference_images: Sequence[Path],
        output_path: Path,
        model: str,
    ) -> Path:
        if not prompt.strip() or not model.strip():
            raise ValueError("Image generation requires a prompt and configured model")
        output_path = Path(output_path)
        if output_path.exists():
            raise FileExistsError(f"Refusing to overwrite image output: {output_path}")
        if not self.api_key and self._client is None:
            raise RuntimeError("Set AVD_GEMINI_API_KEY before generating images")
        if gemini_uses_openai_compat(self.base_url):
            return self._compat_chat(prompt, reference_images, Path(output_path), model)
        return self._native(prompt, reference_images, Path(output_path), model)

    def _native_client(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise RuntimeError("Install avd[llm] to use the Gemini image backend") from exc
            kwargs: dict[str, Any] = {"api_key": self.api_key}
            if self.base_url:
                kwargs["http_options"] = {"base_url": self.base_url}
            self._client = genai.Client(**kwargs)
        return self._client

    def _native(
        self, prompt: str, reference_images: Sequence[Path], output_path: Path, model: str
    ) -> Path:
        parts: list[dict[str, Any]] = []
        for path in reference_images:
            image = Path(path)
            if not image.is_file():
                raise FileNotFoundError(f"Reference image does not exist: {image}")
            mime = mimetypes.guess_type(image.name)[0] or "image/png"
            parts.append({"inline_data": {"mime_type": mime, "data": image.read_bytes()}})
        parts.append({"text": prompt})
        result = self._native_client().models.generate_content(
            model=model,
            contents=[{"role": "user", "parts": parts}],
            config={"response_modalities": ["TEXT", "IMAGE"]},
        )
        content = _gemini_image_bytes(result)
        return _write_png(output_path, content, source="Gemini")

    def _compat_client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install avd[llm] to use the Gemini image backend") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def _compat_chat(
        self, prompt: str, reference_images: Sequence[Path], output_path: Path, model: str
    ) -> Path:
        parts: list[dict[str, Any]] = []
        for path in reference_images:
            image = Path(path)
            if not image.is_file():
                raise FileNotFoundError(f"Reference image does not exist: {image}")
            mime = mimetypes.guess_type(image.name)[0] or "image/png"
            encoded = base64.b64encode(image.read_bytes()).decode()
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
            )
        parts.append({"type": "text", "text": prompt})
        response = self._compat_client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": parts}],
            extra_body={"modalities": ["image", "text"]},
        )
        return _write_png(output_path, _chat_image_bytes(response), source="Gemini")


def _gemini_image_bytes(result: Any) -> bytes:
    candidates = getattr(result, "candidates", None) or []
    parts = []
    if candidates and getattr(candidates[0], "content", None) is not None:
        parts = list(getattr(candidates[0].content, "parts", None) or [])
    notes: list[str] = []
    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        if isinstance(data, str):
            try:
                data = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error):
                data = None
        if isinstance(data, bytes) and _is_image(data):
            return data
        text = getattr(part, "text", None)
        if text:
            notes.append(str(text).strip())
    detail = f": {' '.join(notes)[:240]}" if notes else ""
    raise RuntimeError(f"Gemini returned no image{detail}")


def _chat_image_bytes(response: Any) -> bytes:
    """Read a PNG or JPEG from an OpenAI-compatible chat image response."""

    message = response.choices[0].message
    raw = message.model_dump() if hasattr(message, "model_dump") else {}
    blobs: list[object] = []
    for image in raw.get("images") or []:
        if isinstance(image, dict):
            url = image.get("url") or (image.get("image_url") or {}).get("url")
            if url:
                blobs.append(url)
    content = raw.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            inline = part.get("inline_data") or {}
            if isinstance(inline, dict) and inline.get("data"):
                blobs.append(inline["data"])
            image_url = part.get("image_url") or part.get("image") or {}
            if isinstance(image_url, dict) and image_url.get("url"):
                blobs.append(image_url["url"])
    elif isinstance(content, str):
        blobs.append(content)
    for blob in blobs:
        if isinstance(blob, bytes) and _is_image(blob):
            return blob
        if not isinstance(blob, str) or not blob.strip():
            continue
        encoded = blob.split("base64,", 1)[1] if "base64," in blob else blob
        try:
            content_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            continue
        if _is_image(content_bytes):
            return content_bytes
    raise RuntimeError("Gemini image response did not include an image")


__all__ = [
    "GeminiImageBackend",
    "ImageBackend",
    "ImageGenerationRejectedError",
    "ImageGenerationUncertainError",
    "OpenAIImageBackend",
]
