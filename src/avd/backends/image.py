"""Lazy OpenAI Images adapter with injectable clients for offline tests."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Protocol


class ImageBackend(Protocol):
    def generate(
        self,
        *,
        prompt: str,
        reference_images: Sequence[Path],
        output_path: Path,
        model: str,
    ) -> Path: ...


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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("Set AVD_OPENAI_API_KEY before generating images")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("Install avd[llm] to use the OpenAI image backend") from exc
            self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
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
        arguments = {"model": model, "prompt": prompt, "n": 1, "output_format": "png"}
        if references:
            with ExitStack() as stack:
                handles = [stack.enter_context(path.open("rb")) for path in references]
                response = client.images.edit(image=handles, **arguments)
        else:
            response = client.images.generate(**arguments)
        data = getattr(response, "data", None)
        encoded = getattr(data[0], "b64_json", None) if data else None
        if not isinstance(encoded, str) or not encoded:
            raise RuntimeError("OpenAI Images returned no base64 image")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("OpenAI Images returned invalid base64 image data") from exc
        if not content:
            raise RuntimeError("OpenAI Images returned an empty image")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("xb") as handle:
            handle.write(content)
        return output_path


__all__ = ["ImageBackend", "OpenAIImageBackend"]
