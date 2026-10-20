"""Manual paid image smoke test; requires an explicit prompt and API key."""

import os
from pathlib import Path
import uuid

from avd.backends.image import OpenAIImageBackend
from avd.config import Settings


def main() -> None:
    settings = Settings()
    prompt = os.environ.get("AVD_SMOKE_IMAGE_PROMPT")
    if not settings.openai_api_key or not prompt:
        print("Skipped: set AVD_OPENAI_API_KEY and AVD_SMOKE_IMAGE_PROMPT.")
        return
    reference = os.environ.get("AVD_SMOKE_IMAGE_REFERENCE")
    output = settings.data_dir / "smoke" / "image" / uuid.uuid4().hex / "image.png"
    backend = OpenAIImageBackend(settings.openai_api_key.get_secret_value(), settings.openai_base_url)
    result = backend.generate(
        prompt=prompt, reference_images=[Path(reference)] if reference else [],
        output_path=output, model=settings.image_model,
    )
    print(f"Generated image: {result.resolve()}")


if __name__ == "__main__":
    main()
