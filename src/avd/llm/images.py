"""Image-model routing entry point alongside the text/multimodal LLM router."""

from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from avd.backends.image import GeminiImageBackend, ImageBackend, OpenAIImageBackend
from avd.config import Settings

_IMAGE_LABELS = {"openai": "OpenAI", "gemini": "Gemini", "grok": "Grok"}


def still_image_choice(model_id: str) -> tuple[str, str] | None:
    """Return the provider that should call this still-image model, if it is one."""

    name = model_id.strip()
    lowered = name.lower()
    if not name or "video" in lowered:
        return None
    if lowered.startswith("gpt-image"):
        return "openai", name
    if lowered.startswith("grok-imagine-image"):
        return "grok", name
    if lowered.startswith("gemini") and "image" in lowered:
        return "gemini", name
    return None


def still_image_choices(model_ids: Iterable[str]) -> list[dict[str, str]]:
    """Deduplicate still-image models and label them by the provider that calls them."""

    seen: set[tuple[str, str]] = set()
    choices: list[dict[str, str]] = []
    for model_id in model_ids:
        choice = still_image_choice(model_id)
        if choice is None or choice in seen:
            continue
        seen.add(choice)
        provider, model = choice
        choices.append(
            {
                "provider": provider,
                "model": model,
                "label": f"{_IMAGE_LABELS[provider]} · {model}",
            }
        )
    return choices


def resolve_still_image(
    settings: Settings, provider: str | None, model: str | None
) -> tuple[str, str]:
    """Use an explicit still-image choice, or the configured default."""

    if provider is None and model is None:
        return settings.image_route()
    if not provider or not model or still_image_choice(model) != (provider, model):
        raise ValueError("Choose a still-image model from the available list")
    return provider, model


def build_image_backend(settings: Settings, provider: str | None = None) -> ImageBackend:
    """Build the still-image client for OpenAI, Gemini, or Grok."""

    chosen = provider or settings.image_provider
    if chosen == "gemini":
        key = settings.gemini_api_key
        return GeminiImageBackend(
            key.get_secret_value() if key else None,
            settings.gemini_base_url,
        )
    if chosen == "grok":
        key = settings.grok_api_key
        return OpenAIImageBackend(
            key.get_secret_value() if key else None,
            settings.grok_base_url,
            missing_key="AVD_GROK_API_KEY",
            response_format="b64_json",
            stream_images=False,
        )
    if chosen == "openai":
        key = settings.openai_api_key
        return OpenAIImageBackend(
            key.get_secret_value() if key else None,
            settings.openai_base_url,
        )
    raise ValueError(f"Unknown image provider: {chosen}")


class ImageRouter:
    """Resolve the image provider; executor attempts own durable cost accounting."""

    def __init__(self, settings: Settings, backend: ImageBackend | None = None) -> None:
        self.settings = settings
        self.backend = backend

    def generate(
        self,
        *,
        prompt: str,
        reference_images: Sequence[Path],
        output_path: Path,
        model: str,
        provider: str | None = None,
        request_observer: Callable[[str, str], None] | None = None,
    ) -> Path:
        chosen = provider or self.settings.image_provider
        backend = self.backend or build_image_backend(self.settings, chosen)
        if request_observer is not None:
            request_observer(chosen, model)
        return backend.generate(
            prompt=prompt, reference_images=reference_images, output_path=output_path, model=model
        )
