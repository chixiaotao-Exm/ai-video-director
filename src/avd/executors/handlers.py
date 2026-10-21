"""Translate immutable job payloads into image, ComfyUI and FFmpeg work."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from avd.backends.comfyui import ComfyUIClient
from avd.backends.image import ImageBackend
from avd.backends.media import assemble_final, burn_subtitles, media_duration, write_srt
from avd.backends.workflows import load_workflow, render_workflow
from avd.config import ComfyUIProfile, Settings
from avd.jobs.models import Job
from avd.jobs.worker import HandlerRegistry
from avd.llm.images import ImageRouter


class ComfyBackend(Protocol):
    def upload_file(self, path: str | Path) -> str: ...
    def submit(self, workflow: Mapping[str, Any]) -> str: ...
    def wait(
        self, prompt_id: str, poll_interval: float = 0.25, timeout: float | None = None
    ) -> dict[str, Any]: ...
    def download_outputs(self, prompt_id: str, dest_dir: str | Path) -> list[Path]: ...


def _data_path(root: Path, value: Any, *, existing: bool = False) -> Path:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError("Media paths must be non-empty relative strings")
    portable = PurePosixPath(value.replace("\\", "/"))
    if portable.is_absolute() or PureWindowsPath(value).drive or ".." in portable.parts:
        raise ValueError(f"Media path must stay under data_dir: {value!r}")
    path = (root / portable).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError(f"Media path must stay under data_dir: {value!r}")
    if existing and not path.is_file():
        raise FileNotFoundError(f"Input media does not exist: {path}")
    return path


def _positive(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return result


def _reference_paths(root: Path, value: Any) -> list[Path]:
    if isinstance(value, Mapping):
        values = [value[key] for key in sorted(value)]
    elif isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence):
        values = list(value)
    elif value is None:
        values = []
    else:
        raise ValueError("Reference media must be a path, list or named mapping")
    return [_data_path(root, path, existing=True) for path in values]


class _Execution:
    def __init__(self, job: Job) -> None:
        self.payload = job.payload
        self.root = Path(self.payload["data_dir"]).expanduser().resolve()
        self.recipe: dict[str, Any] = dict(self.payload.get("recipe", {}))
        self.settings: dict[str, Any] = dict(self.recipe.get("_execution", {}))
        self.context: dict[str, Any] = dict(self.payload.get("context", {}))
        self.inputs: list[dict[str, Any]] = list(self.payload.get("inputs", {}).values())
        base = _data_path(self.root, self.payload["output_dir"])
        if base.relative_to(self.root).parts[0] != "projects":
            raise ValueError("Executor output_dir must be inside projects/")
        self.output_dir = base / f"attempt-{job.attempts or 1}"
        self.cost = float(self.payload.get("estimated_cost", 0.0))
        if not math.isfinite(self.cost) or self.cost < 0:
            raise ValueError("estimated_cost must be a nonnegative finite number")

    def prepare(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=False)

    def dependency(self, kind: str) -> dict[str, Any]:
        candidates = [item for item in self.inputs if item.get("kind") == kind]
        if len(candidates) != 1:
            raise ValueError(f"Expected exactly one {kind} input, got {len(candidates)}")
        return candidates[0]

    def input_file(self, item: Mapping[str, Any], name: str) -> Path:
        return _data_path(self.root, item.get("outputs", {}).get(name), existing=True)

    def result(
        self, outputs: Mapping[str, Path], *, duration_s: float | None = None
    ) -> dict[str, Any]:
        paths: dict[str, str] = {}
        for key, value in outputs.items():
            path = Path(value).resolve()
            if not path.is_relative_to(self.output_dir.resolve()) or not path.is_file():
                raise ValueError(f"Executor returned an invalid output path: {path}")
            if path.stat().st_size == 0:
                raise ValueError(f"Executor returned an empty output file: {path}")
            paths[key] = path.relative_to(self.root).as_posix()
        result: dict[str, Any] = {"outputs": paths, "cost": self.cost}
        if duration_s is not None:
            result["duration_s"] = _positive(duration_s, "Media duration")
        return result


def build_registry(
    settings: Settings,
    *,
    image_backend: ImageBackend | None = None,
    comfyui_client: ComfyBackend | None = None,
    before_paid: Callable[[Job], None] | None = None,
    image_request_observer: Callable[[Job, str, str], None] | None = None,
    performance_backend: Any = None,
) -> HandlerRegistry:
    """Create six handlers; external clients can be replaced with offline fakes.

    The rebuild engine checks consent and budget before queueing work and owns
    the ledger. Handlers return their estimated cost exactly once as job data.
    """
    images = ImageRouter(settings, image_backend)
    registry = HandlerRegistry()

    def image_gen(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        recipe = execution.recipe
        character = execution.context.get("character") or {}
        appearance = recipe.get("appearance_prompt", character.get("fixed_appearance_prompt", ""))
        visual = recipe.get("visual_prompt", recipe.get("prompt", ""))
        sheet = recipe.get("character_sheet") is True or recipe.get("mode") == "character_sheet"
        references = _reference_paths(
            execution.root, recipe.get("reference_images", character.get("reference_images", {}))
        )
        if not references and not sheet:
            raise ValueError("First frame generation requires character reference images")
        prompt = "\n\n".join(str(value).strip() for value in (appearance, visual) if value)
        if sheet:
            prompt = (
                "Create a character reference sheet with front, side and full-body views.\n\n"
                + prompt
            )
        if not prompt.strip():
            raise ValueError("Image generation requires an appearance or visual prompt")
        execution.prepare()
        if before_paid is not None:
            before_paid(job)
        provider, default_model = settings.image_route()
        output = images.generate(
            prompt=prompt,
            reference_images=references,
            output_path=execution.output_dir / "image.png",
            model=str(execution.settings.get("image_model", default_model)),
            provider=str(execution.settings.get("image_provider", provider)),
            request_observer=(lambda chosen, model: image_request_observer(job, chosen, model))
            if image_request_observer is not None
            else None,
        )
        return execution.result({"image": output})

    def comfy(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        profile = ComfyUIProfile.model_validate(
            execution.settings.get("comfyui_profile", settings.comfyui_profile)
        )
        owned = comfyui_client is None
        client = comfyui_client or ComfyUIClient(profile.base_url, timeout=profile.timeout)
        try:
            recipe = execution.recipe
            if job.job_type == "image_to_video":
                frame = execution.input_file(execution.dependency("shot_frame"), "image")
                duration = _positive(recipe.get("duration_s", 5.0), "Video duration")
                seed = recipe.get("seed", 0)
                if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
                    raise ValueError("Video seed must be a nonnegative integer")
                values = {
                    "image": client.upload_file(frame),
                    "prompt": str(recipe.get("prompt", recipe.get("camera", ""))),
                    "duration_s": duration,
                    "seed": seed,
                }
                output_kind = "video"
            elif job.job_type == "tts":
                voice = execution.context.get("voice") or {}
                samples = _reference_paths(
                    execution.root,
                    recipe.get("sample_audio_paths", voice.get("sample_audio_paths", [])),
                )
                if not samples:
                    raise ValueError("TTS requires a consented voice sample")
                defaults = recipe.get("default_params", voice.get("default_params", {})) or {}
                speed = _positive(recipe.get("speed", defaults.get("speed", 1.0)), "Voice speed")
                values = {
                    "text": str(recipe.get("line", "")),
                    "voice_sample": client.upload_file(samples[0]),
                    "speed": speed,
                    "emotion": str(recipe.get("emotion", defaults.get("emotion", "neutral"))),
                }
                output_kind = "audio"
            else:
                video = execution.input_file(execution.dependency("shot_video"), "video")
                audio = execution.input_file(execution.dependency("voiceover"), "audio")
                values = {"video": client.upload_file(video), "audio": client.upload_file(audio)}
                output_kind = "video"
            if "workflow_template" in execution.settings:
                if execution.settings.get("workflow_name") != job.job_type:
                    raise ValueError("Captured workflow does not match the job type")
                workflow = render_workflow(execution.settings["workflow_template"], values)
            else:
                workflow = load_workflow(job.job_type, values, template_dir=settings.workflow_dir)
            execution.prepare()
            if before_paid is not None:
                before_paid(job)
            prompt_id = client.submit(workflow)
            client.wait(prompt_id, timeout=profile.timeout)
            downloaded = client.download_outputs(prompt_id, execution.output_dir)
            extensions = (
                (".wav", ".flac", ".mp3", ".m4a", ".ogg", ".aac")
                if output_kind == "audio"
                else (".mp4", ".webm", ".mov", ".mkv", ".avi")
            )
            candidates = [
                Path(path) for path in downloaded if Path(path).suffix.lower() in extensions
            ]
            if not candidates:
                raise RuntimeError(f"ComfyUI returned no {output_kind} output for {job.job_type}")
            candidates.sort(key=lambda path: (extensions.index(path.suffix.lower()), path.name))
            output = candidates[0]
            result = execution.result({output_kind: output})
            result["duration_s"] = _positive(media_duration(output), "Media duration")
            return result
        finally:
            if owned:
                assert isinstance(client, ComfyUIClient)
                client.close()

    def subtitles(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        lip = execution.dependency("lipsync")
        voice = execution.dependency("voiceover")
        video = execution.input_file(lip, "video")
        audio = execution.input_file(voice, "audio")
        duration = _positive(voice.get("duration_s") or media_duration(audio), "Voice duration")
        line = execution.recipe.get("line")
        if line is None:
            shots = (execution.context.get("storyboard") or {}).get("shots", [])
            matching = [shot for shot in shots if shot.get("index") == lip.get("shot_index")]
            if len(matching) != 1:
                raise ValueError("Subtitles require the matching storyboard shot")
            line = matching[0]["line"]
        execution.prepare()
        srt = write_srt([(str(line), duration)], execution.output_dir / "subtitles.srt")
        output = burn_subtitles(video, srt, execution.output_dir / "subtitled.mp4")
        return execution.result({"video": output, "srt": srt}, duration_s=media_duration(output))

    def final(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        items = [item for item in execution.inputs if item.get("kind") == "subtitles"]
        if not items:
            items = [item for item in execution.inputs if item.get("kind") == "lipsync"]
        if not items or any(not isinstance(item.get("shot_index"), int) for item in items):
            raise ValueError("Final render requires shots with integer shot indexes")
        indexes = [item["shot_index"] for item in items]
        if len(set(indexes)) != len(indexes):
            raise ValueError("Final render has duplicate shot indexes")
        clips = [
            execution.input_file(item, "video")
            for item in sorted(items, key=lambda item: item["shot_index"])
        ]
        execution.prepare()
        output = assemble_final(
            clips, execution.output_dir / "final.mp4", ai_label=settings.ai_label
        )
        return execution.result({"final": output}, duration_s=media_duration(output))

    registry.register("image_gen", image_gen)
    for job_type in ("image_to_video", "tts", "lipsync"):
        registry.register(job_type, comfy)
    registry.register("subtitles", subtitles)
    registry.register("final", final)
    from avd.performance.executors import install_performance_handlers

    install_performance_handlers(
        registry, settings, backend=performance_backend, before_paid=before_paid
    )
    return registry


__all__ = ["ComfyBackend", "build_registry"]
