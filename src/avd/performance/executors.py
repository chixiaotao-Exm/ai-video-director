"""Performance jobs share durable scheduling while model adapters remain explicit."""

from __future__ import annotations

import json
import math
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from avd.config import Settings
from avd.jobs.models import Job
from avd.jobs.worker import HandlerRegistry

from .graph import PERFORMANCE_KINDS, file_hash
from .models import PreparationRequest, PreparedPerformance
from .prepare import MediaTools, prepare_performance
from .render import check_performance_sync, mix_performance_audio, render_performance


def media_tools(root: Path) -> MediaTools:
    bundled = root / "tools" / "ffmpeg"
    suffix = ".exe" if os.name == "nt" else ""
    return MediaTools(
        ffmpeg=str(bundled / f"ffmpeg{suffix}")
        if (bundled / f"ffmpeg{suffix}").is_file()
        else "ffmpeg",
        ffprobe=str(bundled / f"ffprobe{suffix}")
        if (bundled / f"ffprobe{suffix}").is_file()
        else "ffprobe",
    )


class PerformanceBackend(Protocol):
    """A real adapter or an explicitly injected offline test backend."""

    def run(
        self,
        stage: str,
        *,
        data_dir: Path,
        recipe: Mapping[str, Any],
        inputs: list[dict[str, Any]],
        context: Mapping[str, Any],
        output_dir: Path,
        before_submit: Callable[[], None] | None = None,
    ) -> Mapping[str, Path]: ...


def install_performance_handlers(
    registry: HandlerRegistry,
    settings: Settings,
    *,
    backend: PerformanceBackend | None = None,
    before_paid: Callable[[Job], None] | None = None,
) -> None:
    from avd.executors.handlers import _Execution

    def prepare(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        request = PreparationRequest.model_validate(
            {
                key: value
                for key, value in execution.recipe.items()
                if key in PreparationRequest.model_fields
            }
        )
        expected = execution.recipe.get("source_sha256")
        if expected and file_hash(execution.root, request.source_video_path) != expected:
            raise ValueError("Source video changed after configuration; configure the current file")
        result = prepare_performance(
            execution.root,
            request,
            output_relative=execution.output_dir.relative_to(execution.root).as_posix(),
            tools=media_tools(execution.root),
        )
        return execution.result(
            {
                "motion": execution.root / result.motion.path,
                "audio": execution.root / result.audio.path,
                "manifest": execution.output_dir / "manifest.json",
            },
            duration_s=result.timeline.actual_duration_s,
        )

    requirements = {
        "vocal_separation": "Configure a versioned vocal-separation backend (Demucs)",
        "singing_voice": "Configure a ready singing voice model and RVC conversion backend",
        "motion_video": "Configure a real motion-transfer workflow and reachable ComfyUI service",
        "performance_lipsync": "Configure a lip-correction workflow or disable lip correction",
    }

    def prepared(execution: _Execution) -> PreparedPerformance:
        item = execution.dependency("performance_prepare")
        manifest = execution.input_file(item, "manifest")
        result = PreparedPerformance.model_validate_json(manifest.read_text(encoding="utf-8"))
        if result.source.sha256 != item["recipe"].get("source_sha256"):
            raise ValueError("Preparation manifest does not match the configured source")
        return result

    def relative(execution: _Execution, kind: str, name: str) -> str:
        return (
            execution.input_file(execution.dependency(kind), name)
            .relative_to(execution.root)
            .as_posix()
        )

    def expected_frames(execution: _Execution) -> int | None:
        value = execution.dependency("motion_video").get("recipe", {}).get("expected_frame_count")
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 1
        ):
            raise ValueError("Model expected_frame_count must be a positive integer")
        return value

    def native_frame_rate(execution: _Execution) -> str | None:
        recipe = execution.dependency("motion_video").get("recipe", {})
        workflow = recipe.get("workflow") or {}
        sampling = workflow.get("spec", {}).get("input_sampling")
        if sampling is None:
            return None
        from .workflow_spec import MotionInputSampling

        return MotionInputSampling.model_validate(sampling).frame_rate

    def gain_db(recipe: Mapping[str, Any], key: str) -> float:
        if f"{key}_db" in recipe:
            return float(recipe[f"{key}_db"])
        # Earlier authored recipes used linear gain. Preserve their exact meaning.
        value = float(recipe.get(key, 1.0))
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Mix gains must be positive and finite")
        return 20 * math.log10(value)

    def mix(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        timeline = prepared(execution)
        result = mix_performance_audio(
            execution.root,
            timeline,
            relative(execution, "singing_voice", "vocals"),
            relative(execution, "vocal_separation", "accompaniment"),
            output_relative=execution.output_dir.relative_to(execution.root).as_posix(),
            vocal_gain_db=gain_db(execution.recipe, "vocal_gain"),
            accompaniment_gain_db=gain_db(execution.recipe, "accompaniment_gain"),
            peak_limit_db=float(execution.recipe.get("peak_limit_db", -1)),
            tools=media_tools(execution.root),
        )
        return execution.result(
            {
                "audio": execution.root / result.audio.path,
                "manifest": execution.output_dir / "mix_manifest.json",
            },
            duration_s=timeline.timeline.actual_duration_s,
        )

    def sync(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        timeline = prepared(execution)
        video = relative(execution, "motion_video", "video")
        result = check_performance_sync(
            execution.root,
            timeline,
            video,
            relative(execution, "singing_voice", "vocals"),
            expected_frame_count=expected_frames(execution),
            native_frame_rate=native_frame_rate(execution),
            tools=media_tools(execution.root),
        )
        execution.prepare()
        # A version owns its files. A copy keeps rollback independent of upstream paths.
        output = execution.output_dir / f"synced{Path(video).suffix}"
        shutil.copyfile(execution.root / video, output)
        if (
            file_hash(execution.root, output.relative_to(execution.root).as_posix())
            != result.video.sha256
        ):
            raise ValueError("Motion video changed during synchronization")
        manifest = execution.output_dir / "sync_manifest.json"
        manifest.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return execution.result(
            {"video": output, "manifest": manifest}, duration_s=timeline.timeline.actual_duration_s
        )

    def final(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        if execution.settings.get("ai_label") is not True:
            raise ValueError(
                "Performance final requires a frozen AI-generated label; rebuild the current recipe"
            )
        timeline = prepared(execution)
        video_kind = (
            "performance_lipsync" if execution.recipe.get("lipsync") else "performance_sync"
        )
        result = render_performance(
            execution.root,
            timeline,
            relative(execution, video_kind, "video"),
            relative(execution, "performance_mix", "audio"),
            output_relative=execution.output_dir.relative_to(execution.root).as_posix(),
            expected_frame_count=expected_frames(execution),
            native_frame_rate=native_frame_rate(execution),
            ai_label=True,
            tools=media_tools(execution.root),
        )
        return execution.result(
            {
                "video": execution.root / result.video.path,
                "manifest": execution.output_dir / "render_manifest.json",
            },
            duration_s=timeline.timeline.actual_duration_s,
        )

    def run(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        if backend is None:
            raise RuntimeError(requirements[job.job_type])
        execution.prepare()
        remote_motion = job.job_type in {"motion_video", "performance_lipsync"}
        if before_paid and job.job_type == "singing_voice":
            before_paid(job)
        outputs = backend.run(
            job.job_type,
            data_dir=execution.root,
            recipe=execution.recipe,
            inputs=execution.inputs,
            context=execution.context,
            output_dir=execution.output_dir,
            before_submit=(lambda: before_paid(job)) if remote_motion and before_paid else None,
        )
        if remote_motion:
            manifest = outputs.get("manifest")
            if manifest is not None and json.loads(manifest.read_text(encoding="utf-8")).get(
                "submission_reused"
            ):
                execution.cost = 0
        return execution.result(outputs)

    registry.register("performance_prepare", prepare)
    registry.register("performance_mix", mix)
    registry.register("performance_sync", sync)
    registry.register("performance_final", final)
    image = registry.get("image_gen")
    assert image is not None

    def scene(job: Job) -> dict[str, Any]:
        execution = _Execution(job)
        references = execution.recipe.get("reference_images", {})
        hashes = execution.recipe.get("reference_hashes", {})
        if not references or set(references) != set(hashes):
            raise ValueError("Configure character reference hashes before scene generation")
        for name, path in references.items():
            if file_hash(execution.root, path) != hashes[name]:
                raise ValueError(
                    "Character reference changed; configure and review the current images"
                )
        return image(job)

    registry.register("scene_reference", scene)
    for kind in PERFORMANCE_KINDS:
        if kind.value in requirements:
            registry.register(kind.value, run)
