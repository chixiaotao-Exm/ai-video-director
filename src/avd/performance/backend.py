"""Production singing adapters, constructed with the worker's own database session."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from avd.backends.comfyui import ComfyUIClient
from avd.config import ComfyUIProfile, Settings
from avd.state.consent import require_usable
from avd.state.models import Voice

from .models import PreparedPerformance
from .motion import MotionStage, run_motion_workflow
from .seed_vc import SeedVCVoiceConverter, validate_seed_snapshot
from .voice import (
    RVCInstallation,
    RVCParameters,
    RVCVoiceConverter,
    SeedVCInstallation,
    SeedVCParameters,
    SingingVoiceError,
    SingingVoiceModelVersion,
    validate_rvc_snapshot,
)


class LocalPerformanceBackend:
    def __init__(self, settings: Settings, session_factory: Callable[[], Session]) -> None:
        self.settings = settings
        self.session_factory = session_factory

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
    ) -> Mapping[str, Path]:
        from avd.executors.handlers import _data_path

        def dependency(kind: str, key: str) -> Path:
            matches = [item for item in inputs if item.get("kind") == kind]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {kind} input")
            return _data_path(data_dir, matches[0].get("outputs", {}).get(key), existing=True)

        if stage in {"motion_video", "performance_lipsync"}:
            from .executors import media_tools

            workflow = recipe.get("workflow")
            profile = recipe.get("comfyui_profile")
            if not isinstance(workflow, dict) or not isinstance(profile, dict):
                raise ValueError(
                    "Configure and freeze a real performance workflow before generation"
                )
            connection = ComfyUIProfile.model_validate(profile)
            files = (
                {
                    "motion_video": dependency("performance_prepare", "motion"),
                    "reference_image": dependency("scene_reference", "image"),
                }
                if stage == "motion_video"
                else {
                    "motion_video": dependency("performance_sync", "video"),
                    "converted_vocals": dependency("singing_voice", "vocals"),
                }
            )
            with ComfyUIClient(connection.base_url, timeout=connection.timeout) as client:
                return run_motion_workflow(
                    stage=cast(MotionStage, stage),
                    data_dir=data_dir,
                    workflow_snapshot=workflow,
                    inputs=files,
                    output_dir=output_dir,
                    client=client,
                    tools=media_tools(data_dir),
                    parameters=recipe.get("parameters") or {},
                    journal_path=output_dir.parent / "remote-submission.json",
                    before_submit=before_submit,
                )

        if stage == "singing_voice":
            version_id = recipe.get("voice_model_version_id")
            if not isinstance(version_id, str) or not version_id:
                raise SingingVoiceError(
                    "Select a prepared singing model version; a TTS sample alone is not ready"
                )
            runtime_key = (
                "seed_vc_runtime" if recipe.get("voice_engine") == "seed-vc" else "rvc_runtime"
            )
            snapshot = recipe.get("_execution", {}).get(runtime_key)
            if not isinstance(snapshot, dict) or not snapshot.get("installation"):
                raise SingingVoiceError("Singing runtime snapshot is missing; create a new recipe")
            if runtime_key == "seed_vc_runtime":
                validate_seed_snapshot(
                    SeedVCInstallation.model_validate(snapshot["installation"]), snapshot
                )
            else:
                validate_rvc_snapshot(
                    RVCInstallation.model_validate(snapshot["installation"]), snapshot
                )
            vocals = dependency("vocal_separation", "vocals")
            output = output_dir / "converted.wav"
            with self.session_factory() as session:
                model = session.get(SingingVoiceModelVersion, version_id)
                voice_id = context.get("voice", {}).get("id")
                if (
                    model is None
                    or model.voice_id != voice_id
                    or model.voice_id != recipe.get("voice_id")
                ):
                    raise SingingVoiceError(
                        "The selected singing model does not belong to the target voice"
                    )
                voice = session.get(Voice, model.voice_id)
                if voice is None:
                    raise SingingVoiceError("The target voice no longer exists")
                require_usable(voice)
                expected = recipe.get("voice_model_fingerprint")
                if not expected or expected != model.fingerprint:
                    raise SingingVoiceError(
                        "Singing model changed; configure the selected version again"
                    )
                if recipe.get("voice_engine", model.engine) != model.engine:
                    raise SingingVoiceError(
                        "The frozen singing engine differs from the model version"
                    )
                execution = recipe.get("_execution", {})
                values = recipe.get("parameters") or model.parameters
                if model.engine == "seed-vc":
                    snapshot = execution.get("seed_vc_runtime")
                    if not isinstance(snapshot, dict) or not snapshot.get("installation"):
                        raise SingingVoiceError(
                            "SeedVC runtime snapshot is missing; create a new recipe"
                        )
                    installation = SeedVCInstallation.model_validate(snapshot["installation"])
                    validate_seed_snapshot(installation, snapshot)
                    if snapshot != model.runtime_snapshot:
                        raise SingingVoiceError(
                            "SeedVC recipe and voice version freeze different runtimes"
                        )
                    source_versions = [
                        item.get("version_id")
                        for item in inputs
                        if item.get("kind") == "vocal_separation"
                    ]
                    result = SeedVCVoiceConverter(data_dir, installation).convert(
                        session,
                        version_id,
                        vocals.relative_to(data_dir).as_posix(),
                        output.relative_to(data_dir).as_posix(),
                        parameters=SeedVCParameters.model_validate(values),
                        source_version_id=source_versions[0] if source_versions else None,
                    )
                elif model.engine == "rvc":
                    snapshot = execution.get("rvc_runtime")
                    if not isinstance(snapshot, dict) or not snapshot.get("installation"):
                        raise SingingVoiceError(
                            "RVC runtime snapshot is missing; create a new recipe"
                        )
                    rvc_installation = RVCInstallation.model_validate(snapshot["installation"])
                    validate_rvc_snapshot(rvc_installation, snapshot)
                    result = RVCVoiceConverter(data_dir, rvc_installation).convert(
                        session,
                        version_id,
                        vocals.relative_to(data_dir).as_posix(),
                        output.relative_to(data_dir).as_posix(),
                        parameters=RVCParameters.model_validate(values),
                    )
                else:
                    raise SingingVoiceError("Unsupported singing conversion engine")
            return {"vocals": output, "manifest": data_dir / result["manifest_path"]}

        if stage == "vocal_separation":
            from .executors import media_tools
            from .separation import (
                find_library_stems,
                separate_performance,
                validate_demucs_snapshot,
            )

            prepared = PreparedPerformance.model_validate_json(
                dependency("performance_prepare", "manifest").read_text(encoding="utf-8")
            )
            tools = media_tools(data_dir)
            with self.session_factory() as session:
                snapshot = recipe.get("library_stems")
                stems = (
                    find_library_stems(
                        session, data_dir, prepared, expected_snapshot=snapshot, tools=tools
                    )
                    if snapshot
                    else None
                )
            separated = separate_performance(
                data_dir,
                prepared,
                library_stems=stems,
                demucs=validate_demucs_snapshot(data_dir, recipe.get("demucs") or {})
                if stems is None
                else None,
                output_relative=(output_dir / "stems").relative_to(data_dir).as_posix(),
                tools=tools,
            )
            return {
                "vocals": data_dir / separated.vocals.path,
                "accompaniment": data_dir / separated.accompaniment.path,
                "manifest": output_dir / "stems" / "separation_manifest.json",
            }
        requirements = {
            "motion_video": "Configure a real motion-transfer workflow and reachable ComfyUI service",
            "performance_lipsync": "Configure a lip-correction workflow or disable lip correction",
        }
        raise RuntimeError(requirements.get(stage, f"Unsupported performance stage: {stage}"))
