"""Persist the performance graph without reusing the script/TTS pipeline."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from avd.state.consent import require_usable
from avd.state.models import Character, Node, NodeKind, Project, Voice
from avd.state.paths import relative_path
from avd.state.repo import ProjectRepo

from .models import PerformanceConfig
from .motion import MotionStage, snapshot_motion_workflow, validate_motion_snapshot
from .separation import DemucsConfig, library_stem_snapshot, snapshot_demucs
from .voice import RVCParameters, SeedVCParameters, SingingVoiceModelVersion
from .workflow_spec import MotionWorkflowSpec

PERFORMANCE_KINDS = frozenset(
    {
        NodeKind.PERFORMANCE_PREPARE,
        NodeKind.VOCAL_SEPARATION,
        NodeKind.SCENE_REFERENCE,
        NodeKind.SINGING_VOICE,
        NodeKind.MOTION_VIDEO,
        NodeKind.PERFORMANCE_MIX,
        NodeKind.PERFORMANCE_SYNC,
        NodeKind.PERFORMANCE_LIPSYNC,
        NodeKind.PERFORMANCE_FINAL,
    }
)


def file_hash(root: Path, value: str) -> str:
    root = root.resolve()
    path = (root / relative_path(value)).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Performance input must be an existing file under data_dir")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def configure_performance(
    session: Session,
    project: Project,
    config: PerformanceConfig,
    data_dir: Path,
    *,
    comfyui_profile: Mapping[str, Any] | None = None,
    preserve_workflows: set[MotionStage] | None = None,
    preserve_separation: bool = False,
    demucs_config: DemucsConfig | None = None,
) -> dict[str, Node]:
    """Create or update only affected recipes; historical versions remain immutable."""
    if project.mode != "performance_remix":
        raise ValueError("Create a performance_remix project before configuring a performance")
    if any(node.kind not in PERFORMANCE_KINDS for node in project.nodes):
        raise ValueError("A performance project cannot contain the script/TTS graph")
    character = session.get(Character, config.character_id)
    voice = session.get(Voice, config.voice_id)
    if character is None or voice is None:
        raise ValueError("Select an existing character and voice")
    require_usable(character)
    require_usable(voice)
    if not character.reference_images:
        raise ValueError("The target character needs reference images")
    refs = dict(character.reference_images)
    ref_hashes = {key: file_hash(data_dir, path) for key, path in refs.items()}
    source_hash = file_hash(data_dir, config.preparation.source_video_path)
    workflow_recipes: dict[str, dict[str, Any]] = {}
    selections: tuple[tuple[MotionStage, MotionWorkflowSpec | None, dict[str, Any]], ...] = (
        ("motion_video", config.motion_workflow, config.motion_parameters),
        ("performance_lipsync", config.lipsync_workflow, config.lipsync_parameters),
    )
    for stage, spec, parameters in selections:
        snapshot = None
        existing = next((n for n in project.nodes if n.kind.value == stage), None)
        preserved = (
            existing.recipe
            if stage in (preserve_workflows or set())
            and existing is not None
            and existing.recipe.get("workflow") is not None
            else None
        )
        if preserved is not None:
            snapshot = preserved["workflow"]
            validate_motion_snapshot(snapshot, stage, parameters)
            profile = preserved.get("comfyui_profile")
            if not isinstance(profile, dict):
                raise ValueError("The frozen performance workflow has no ComfyUI profile")
        elif spec is not None:
            from avd.config import ComfyUIProfile

            snapshot = snapshot_motion_workflow(spec, stage)
            validate_motion_snapshot(snapshot, stage, parameters)
            captured_profile = comfyui_profile
            if captured_profile is None:
                captured_profile = existing.recipe.get("comfyui_profile") if existing else None
            if captured_profile is None:
                raise ValueError(
                    "Capture a ComfyUI profile when configuring a performance workflow"
                )
            profile = ComfyUIProfile.model_validate(captured_profile).model_dump(mode="json")
        else:
            profile = None
            if parameters:
                raise ValueError("Workflow parameters require a selected performance workflow")
        workflow_recipes[stage] = {
            "workflow": snapshot,
            "parameters": parameters,
            "comfyui_profile": profile,
            "expected_frame_count": preserved.get("expected_frame_count")
            if preserved is not None
            else spec.expected_frame_count
            if spec
            else None,
        }
    voice_model = None
    voice_parameters = None
    if config.voice_model_version_id:
        voice_model = session.get(SingingVoiceModelVersion, config.voice_model_version_id)
        if voice_model is None or voice_model.voice_id != voice.id:
            raise ValueError("Select a singing model version belonging to the target voice")
        parameter_type = SeedVCParameters if voice_model.engine == "seed-vc" else RVCParameters
        normalized_parameters = parameter_type.model_validate(
            {
                **voice_model.parameters,
                **(
                    config.voice_parameters.model_dump(exclude_unset=True)
                    if config.voice_parameters is not None
                    else {}
                ),
            }
        )
        voice_parameters = normalized_parameters.model_dump(mode="json")
        if config.voice_parameters is not None:
            config.voice_parameters = normalized_parameters
    repo = ProjectRepo(session)
    nodes = {node.kind.value: node for node in project.nodes}

    def node(kind: NodeKind, recipe: dict[str, Any], parents: list[Node]) -> Node:
        current = nodes.get(kind.value)
        if current is None:
            current = repo.add_node(project, kind, shot_index=1, recipe=recipe, depends_on=parents)
            nodes[kind.value] = current
        else:
            old_parents = {p.id for p in current.depends_on}
            if old_parents != {p.id for p in parents}:
                current.depends_on = parents
                repo.update_recipe(current, {})
            # update_recipe merges patches; supply only owned keys, preserving execution metadata.
            if any(current.recipe.get(key) != value for key, value in recipe.items()):
                repo.update_recipe(current, recipe)
        return current

    prepare = node(
        NodeKind.PERFORMANCE_PREPARE,
        {
            **config.preparation.model_dump(mode="json"),
            "source_sha256": source_hash,
        },
        [],
    )
    previous_stems = nodes.get(NodeKind.VOCAL_SEPARATION.value)
    if preserve_separation and previous_stems is not None:
        separation_recipe = {
            key: value for key, value in previous_stems.recipe.items() if not key.startswith("_")
        }
    else:
        stem_snapshot = library_stem_snapshot(
            session, data_dir, config.preparation.source_video_path
        )
        separation_recipe = {
            "backend": "library" if stem_snapshot else "demucs",
            "model": demucs_config.model_name
            if demucs_config and not stem_snapshot
            else "htdemucs",
            "library_stems": stem_snapshot,
            "demucs": snapshot_demucs(data_dir, demucs_config) if stem_snapshot is None else None,
        }
    stems = node(
        NodeKind.VOCAL_SEPARATION,
        separation_recipe,
        [prepare],
    )
    scene = node(
        NodeKind.SCENE_REFERENCE,
        {
            "character_id": character.id,
            "reference_images": refs,
            "reference_hashes": ref_hashes,
            "appearance_prompt": character.fixed_appearance_prompt,
            "visual_prompt": config.scene_prompt,
        },
        [],
    )
    singing = node(
        NodeKind.SINGING_VOICE,
        {
            "voice_id": voice.id,
            "voice_model_version_id": config.voice_model_version_id,
            "voice_model_fingerprint": voice_model.fingerprint if voice_model else None,
            "voice_engine": voice_model.engine if voice_model else None,
            "voice_reference_sha256": voice_model.reference_sha256 if voice_model else None,
            "parameters": voice_parameters,
        },
        [stems],
    )
    motion = node(
        NodeKind.MOTION_VIDEO,
        {"preserve_timing": True, **workflow_recipes["motion_video"]},
        [prepare, scene],
    )
    existing_mix = nodes.get(NodeKind.PERFORMANCE_MIX.value)
    mix_recipe = (
        {key: value for key, value in existing_mix.recipe.items() if not key.startswith("_")}
        if existing_mix is not None
        else {"vocal_gain": 1.0, "accompaniment_gain": 1.0}
    )
    if config.mix is not None:
        mix_recipe.update(config.mix.model_dump(mode="json"))
    mix = node(NodeKind.PERFORMANCE_MIX, mix_recipe, [stems, singing])
    sync = node(NodeKind.PERFORMANCE_SYNC, {"preserve_timing": True}, [prepare, motion, singing])
    selected = sync
    if config.lipsync:
        selected = node(
            NodeKind.PERFORMANCE_LIPSYNC,
            workflow_recipes["performance_lipsync"],
            [sync, singing],
        )
    node(NodeKind.PERFORMANCE_FINAL, {"lipsync": config.lipsync}, [selected, mix])
    project.performance_config = config.model_dump(mode="json")
    project.source_video_path = config.preparation.source_video_path
    project.character = character
    project.voice = voice
    project.character_id, project.voice_id = character.id, voice.id
    session.flush()
    return nodes
