"""Executor wiring, immutable paths and dependency selection without services."""

from pathlib import Path
from typing import Any

import pytest

from avd.config import Settings
from avd.executors import build_registry, handlers
from avd.jobs.models import Job


class FakeImage:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate(self, **kwargs: Any) -> Path:
        self.calls.append(kwargs)
        output = kwargs["output_path"]
        with output.open("xb") as handle:
            handle.write(b"fake-png")
        return output


class FakeComfy:
    def __init__(self, extension: str = ".mp4") -> None:
        self.uploads: list[Path] = []
        self.workflows: list[dict] = []
        self.extension = extension

    def upload_file(self, path: Path) -> str:
        self.uploads.append(path)
        return "uploaded-" + path.name

    def submit(self, workflow: dict) -> str:
        self.workflows.append(workflow)
        return "fake-prompt"

    def wait(self, prompt_id: str, **_: Any) -> dict:
        assert prompt_id == "fake-prompt"
        return {"outputs": {}}

    def download_outputs(self, prompt_id: str, dest_dir: Path) -> list[Path]:
        path = dest_dir / ("result" + self.extension)
        with path.open("xb") as handle:
            handle.write(b"fake-media")
        return [path]


def file(root: Path, relative: str) -> str:
    output = root / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"source-media")
    return relative


def job(
    root: Path,
    job_type: str,
    recipe: dict | None = None,
    inputs: dict | None = None,
    context: dict | None = None,
    **payload: Any,
) -> Job:
    return Job(
        job_type=job_type,
        attempts=1,
        payload={
            "data_dir": str(root),
            "output_dir": "projects/project/node/1-version",
            "recipe": recipe or {},
            "inputs": inputs or {},
            "context": context or {},
            "estimated_cost": 0.25,
            **payload,
        },
    )


def run(registry: Any, work: Job) -> dict:
    handler = registry.get(work.job_type)
    assert handler is not None
    return handler(work)


def test_frame_uses_all_character_refs_and_appearance(tmp_path: Path) -> None:
    backend = FakeImage()
    references = {
        "front": file(tmp_path, "assets/front.png"),
        "side": file(tmp_path, "assets/side.png"),
    }
    registry = build_registry(
        Settings(data_dir=tmp_path, image_model="my-image", _env_file=None), image_backend=backend
    )
    work = job(
        tmp_path,
        "image_gen",
        {
            "reference_images": references,
            "appearance_prompt": "red coat",
            "visual_prompt": "street",
        },
    )
    result = run(registry, work)
    assert result["cost"] == 0.25
    assert result["outputs"]["image"] == "projects/project/node/1-version/attempt-1/image.png"
    assert backend.calls[0]["model"] == "my-image"
    assert backend.calls[0]["prompt"] == "red coat\n\nstreet"
    assert backend.calls[0]["reference_images"] == [tmp_path / path for path in references.values()]
    with pytest.raises(FileExistsError):
        run(registry, work)
    assert len(backend.calls) == 1
    work.attempts = 2
    second = run(registry, work)
    assert "attempt-2" in second["outputs"]["image"]
    assert (tmp_path / result["outputs"]["image"]).read_bytes() == b"fake-png"


def test_character_sheet_and_frame_requires_references(tmp_path: Path) -> None:
    backend = FakeImage()
    registry = build_registry(Settings(_env_file=None), image_backend=backend)
    with pytest.raises(ValueError, match="reference images"):
        run(registry, job(tmp_path, "image_gen", {"visual_prompt": "street"}))
    run(
        registry,
        job(tmp_path, "image_gen", {"mode": "character_sheet", "appearance_prompt": "robot"}),
    )
    assert backend.calls[0]["reference_images"] == []
    assert "reference sheet" in backend.calls[0]["prompt"]


@pytest.mark.parametrize(
    "unsafe", ["../outside", "C:/outside", "/outside", "projects/../../outside"]
)
def test_unsafe_output_paths_rejected_before_backend(tmp_path: Path, unsafe: str) -> None:
    backend = FakeImage()
    with pytest.raises(ValueError, match="data_dir"):
        run(
            build_registry(Settings(_env_file=None), image_backend=backend),
            job(tmp_path, "image_gen", {"mode": "character_sheet"}, output_dir=unsafe),
        )
    assert not backend.calls


def test_unsafe_reference_rejected_before_backend(tmp_path: Path) -> None:
    backend = FakeImage()
    with pytest.raises(ValueError, match="data_dir"):
        run(
            build_registry(Settings(_env_file=None), image_backend=backend),
            job(tmp_path, "image_gen", {"reference_images": {"front": "../secret.png"}}),
        )
    assert not backend.calls


@pytest.mark.parametrize(
    "kind,extension", [("image_to_video", ".mp4"), ("tts", ".wav"), ("lipsync", ".mp4")]
)
def test_comfy_uploads_dependencies_and_typed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, extension: str
) -> None:
    backend = FakeComfy(extension)
    monkeypatch.setattr(handlers, "media_duration", lambda _: 2.75)
    inputs = {
        "frame": {
            "kind": "shot_frame",
            "shot_index": 1,
            "outputs": {"image": file(tmp_path, "frame.png")},
        },
        "video": {
            "kind": "shot_video",
            "shot_index": 1,
            "outputs": {"video": file(tmp_path, "video.mp4")},
        },
        "voice": {
            "kind": "voiceover",
            "shot_index": 1,
            "outputs": {"audio": file(tmp_path, "voice.wav")},
        },
    }
    recipe = {
        "camera": "slow pan",
        "duration_s": 3,
        "seed": 42,
        "line": "hello",
        "sample_audio_paths": [file(tmp_path, "sample.wav")],
        "default_params": {"speed": 1.2},
        "emotion": "happy",
    }
    result = run(
        build_registry(Settings(_env_file=None), comfyui_client=backend),
        job(tmp_path, kind, recipe, inputs),
    )
    values = backend.workflows[0]["1"]["inputs"]
    if kind == "image_to_video":
        assert values == {
            "image": "uploaded-frame.png",
            "prompt": "slow pan",
            "duration_s": 3.0,
            "seed": 42,
        }
    elif kind == "tts":
        assert values == {
            "text": "hello",
            "voice_sample": "uploaded-sample.wav",
            "speed": 1.2,
            "emotion": "happy",
        }
    else:
        assert values == {"video": "uploaded-video.mp4", "audio": "uploaded-voice.wav"}
    assert result["duration_s"] == 2.75
    assert result["cost"] == 0.25
    assert all(not Path(path).is_absolute() for path in result["outputs"].values())


def test_subtitles_use_actual_voice_duration_and_storyboard_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    def write(lines: list, dest: Path) -> Path:
        captured.extend(lines)
        dest.write_text("subtitle", encoding="utf-8")
        return dest

    def burn(video: Path, srt: Path, dest: Path) -> Path:
        dest.write_bytes(b"subtitled")
        return dest

    monkeypatch.setattr(handlers, "write_srt", write)
    monkeypatch.setattr(handlers, "burn_subtitles", burn)
    monkeypatch.setattr(handlers, "media_duration", lambda _: 2.75)
    inputs = {
        "lip": {
            "kind": "lipsync",
            "shot_index": 2,
            "outputs": {"video": file(tmp_path, "lip.mp4")},
        },
        "voice": {
            "kind": "voiceover",
            "shot_index": 2,
            "duration_s": 2.25,
            "outputs": {"audio": file(tmp_path, "voice.wav")},
        },
    }
    context = {
        "storyboard": {"shots": [{"index": 1, "line": "first"}, {"index": 2, "line": "second"}]}
    }
    result = run(
        build_registry(Settings(_env_file=None)),
        job(tmp_path, "subtitles", {"duration_s": 99}, inputs, context),
    )
    assert captured == [("second", 2.25)]
    assert set(result["outputs"]) == {"video", "srt"}


def test_final_orders_every_subtitled_shot_and_ignores_ancestors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = []

    def assemble(clips: list[Path], dest: Path) -> Path:
        captured.extend(path.name for path in clips)
        dest.write_bytes(b"final")
        return dest

    monkeypatch.setattr(handlers, "assemble_final", assemble)
    monkeypatch.setattr(handlers, "media_duration", lambda _: 6.0)
    inputs = {
        "second": {
            "kind": "subtitles",
            "shot_index": 2,
            "outputs": {"video": file(tmp_path, "second.mp4")},
        },
        "first": {
            "kind": "subtitles",
            "shot_index": 1,
            "outputs": {"video": file(tmp_path, "first.mp4")},
        },
        "ancestor": {
            "kind": "lipsync",
            "shot_index": 1,
            "outputs": {"video": file(tmp_path, "ancestor.mp4")},
        },
    }
    result = run(build_registry(Settings(_env_file=None)), job(tmp_path, "final", inputs=inputs))
    assert captured == ["first.mp4", "second.mp4"]
    assert set(result["outputs"]) == {"final"}
