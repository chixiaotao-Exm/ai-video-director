"""Gemini video content is sent as media and remote uploads are always cleaned up."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from avd.llm.providers.gemini import GeminiProvider


class FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            candidates=[SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text="video seen", function_call=None)]),
                finish_reason="STOP",
            )],
            usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=2),
        )


class FakeFiles:
    def __init__(self, state: str = "ACTIVE") -> None:
        self.state = state
        self.uploads: list[dict[str, Any]] = []
        self.deleted: list[str] = []

    def upload(self, **kwargs: Any) -> SimpleNamespace:
        self.uploads.append(kwargs)
        return SimpleNamespace(name="files/video-1", uri="https://files.invalid/video-1", state=SimpleNamespace(name=self.state))

    def delete(self, *, name: str) -> None:
        self.deleted.append(name)


def messages(video: Path) -> list[dict[str, Any]]:
    return [{"role": "user", "content": [
        {"type": "text", "text": "Describe structure, not identity."},
        {"type": "video", "path": str(video), "mime_type": "video/mp4"},
    ]}]


def test_small_video_is_transmitted_as_inline_bytes(tmp_path: Path) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"test-video-content")
    models = FakeModels()
    files = FakeFiles()
    provider = GeminiProvider(client=SimpleNamespace(models=models, files=files))
    assert provider.chat("configured-gemini", messages(video)).content == "video seen"
    assert models.calls[0]["contents"][0]["parts"][1] == {
        "inline_data": {"mime_type": "video/mp4", "data": b"test-video-content"}
    }
    assert files.uploads == []


@pytest.mark.parametrize("state", ["ACTIVE", "FAILED"])
def test_large_video_upload_is_cleaned_up_on_success_or_failure(tmp_path: Path, state: str) -> None:
    video = tmp_path / "large.mp4"
    with video.open("wb") as stream:
        stream.truncate(20_000_001)
    models = FakeModels()
    files = FakeFiles(state)
    provider = GeminiProvider(client=SimpleNamespace(models=models, files=files))
    if state == "FAILED":
        with pytest.raises(RuntimeError, match="could not process video"):
            provider.chat("configured-gemini", messages(video))
        assert models.calls == []
    else:
        provider.chat("configured-gemini", messages(video))
        assert models.calls[0]["contents"][0]["parts"][1] == {
            "file_data": {"mime_type": "video/mp4", "file_uri": "https://files.invalid/video-1"}
        }
    assert files.uploads == [{"file": str(video), "config": {"mime_type": "video/mp4"}}]
    assert files.deleted == ["files/video-1"]
