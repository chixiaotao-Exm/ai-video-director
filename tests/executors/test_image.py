"""OpenAI image calls are exercised only through an in-memory client."""

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from avd.backends.image import OpenAIImageBackend


class FakeImages:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _response(self, method: str, arguments: dict) -> SimpleNamespace:
        images = arguments.pop("image", [])
        self.calls.append({"method": method, "references": [image.read() for image in images], **arguments})
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b"png-bytes").decode())])

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
    output = adapter.generate(prompt="Same character outside", reference_images=refs,
                              output_path=tmp_path / "result.png", model="configured-image-model")
    assert output.read_bytes() == b"png-bytes"
    assert images.calls == [{"method": "edit", "references": [b"reference-0", b"reference-1"],
                             "prompt": "Same character outside", "model": "configured-image-model",
                             "n": 1, "output_format": "png"}]
    with pytest.raises(FileExistsError):
        adapter.generate(prompt="again", reference_images=[], output_path=output, model="test")
    assert len(images.calls) == 1


def test_sheet_uses_generation_and_missing_key_is_lazy(tmp_path: Path) -> None:
    adapter = OpenAIImageBackend()
    with pytest.raises(RuntimeError, match="AVD_OPENAI_API_KEY"):
        adapter.generate(prompt="sheet", reference_images=[], output_path=tmp_path / "missing.png", model="test")
    images = FakeImages()
    OpenAIImageBackend(client=SimpleNamespace(images=images)).generate(
        prompt="sheet", reference_images=[], output_path=tmp_path / "sheet.png", model="test"
    )
    assert images.calls[0]["method"] == "generate"


@pytest.mark.parametrize("encoded", [None, "not!base64", ""])
def test_invalid_image_response_does_not_create_output(tmp_path: Path, encoded: str | None) -> None:
    client = SimpleNamespace(images=SimpleNamespace(generate=lambda **_: SimpleNamespace(
        data=[SimpleNamespace(b64_json=encoded)])))
    with pytest.raises(RuntimeError):
        OpenAIImageBackend(client=client).generate(
            prompt="sheet", reference_images=[], output_path=tmp_path / "image.png", model="test"
        )
    assert not (tmp_path / "image.png").exists()
