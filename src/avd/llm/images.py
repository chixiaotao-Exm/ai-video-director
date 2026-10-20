"""Image-model routing entry point alongside the text/multimodal LLM router."""

from collections.abc import Sequence
from pathlib import Path

from avd.backends.image import ImageBackend, OpenAIImageBackend
from avd.config import Settings


class ImageRouter:
    """Resolve the image provider; executor attempts own durable cost accounting."""

    def __init__(self, settings: Settings, backend: ImageBackend | None = None) -> None:
        self.settings = settings
        self.backend = backend or OpenAIImageBackend(
            settings.openai_api_key.get_secret_value() if settings.openai_api_key else None,
            settings.openai_base_url,
        )

    def generate(
        self, *, prompt: str, reference_images: Sequence[Path], output_path: Path, model: str
    ) -> Path:
        return self.backend.generate(
            prompt=prompt, reference_images=reference_images, output_path=output_path, model=model
        )
