"""Generate an original storyboard and apply targeted edits to the shot graph."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from avd.agents.analysis.models import StructureTemplate
from avd.config import Settings
from avd.llm import LLMRouter
from avd.state.consent import require_usable
from avd.state.models import Character, Node, NodeKind, Project, Voice
from avd.state.repo import ProjectRepo


class StoryboardShot(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)

    index: int = Field(ge=1)
    duration_s: float = Field(gt=0)
    line: str
    visual_prompt: str = Field(min_length=1)
    camera: str = Field(min_length=1)
    emotion: str = Field(min_length=1)


class Storyboard(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    shots: list[StoryboardShot] = Field(min_length=1)

    @model_validator(mode="after")
    def contiguous_indexes(self) -> Self:
        if [shot.index for shot in self.shots] != list(range(1, len(self.shots) + 1)):
            raise ValueError("Shot indexes must be unique, contiguous and ordered from 1")
        return self


def write_storyboard(
    template: StructureTemplate,
    character: Character,
    voice: Voice,
    brief: str,
    *,
    router: LLMRouter,
    project: Project | None = None,
) -> Storyboard:
    """Request original dialogue and visuals, never copying the reference person."""

    require_usable(character)
    require_usable(voice)
    if not brief.strip():
        raise ValueError("A creative brief is required")
    context = {
        "structure": template.model_dump(mode="json"),
        "character": {
            "name": character.name,
            "appearance_prompt": character.fixed_appearance_prompt,
        },
        "voice": {"name": voice.name, "default_params": voice.default_params},
        "brief": brief,
    }
    return router.structured(
        "script",
        [
            {
                "role": "system",
                "content": (
                    "Write an original storyboard using only the supplied reference structure and style. "
                    "Use the separately consented character and voice. Never copy the source person's "
                    "identity, face, voice or verbatim lines. Use contiguous shot indexes starting at 1. "
                    "The user must review this draft before any frames are generated."
                ),
            },
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
        Storyboard,
        project=project,
    )


class ScriptAgent:
    def __init__(
        self, repo: ProjectRepo, router: LLMRouter, settings: Settings | None = None
    ) -> None:
        self.repo = repo
        self.router = router
        self.settings = settings or router.settings

    def write_storyboard(
        self,
        template: StructureTemplate,
        character: Character,
        voice: Voice,
        brief: str,
        *,
        project: Project | None = None,
    ) -> Storyboard:
        return write_storyboard(
            template, character, voice, brief, router=self.router, project=project
        )

    def apply_storyboard(
        self, project: Project, storyboard: Storyboard, *, cost: float = 0.0
    ) -> dict[str, Any]:
        return apply_storyboard(project, storyboard, repo=self.repo, cost=cost)

    def update_shot(self, project: Project, shot_index: int, changes: dict[str, Any]) -> list[Node]:
        return update_shot(project, shot_index, changes, repo=self.repo)


def _assets(project: Project) -> tuple[Character, Voice]:
    if project.character is None or project.voice is None:
        raise ValueError("Attach a consented character and voice before applying a storyboard")
    require_usable(project.character)
    require_usable(project.voice)
    return project.character, project.voice


def _existing_graph(project: Project, count: int) -> dict[str, Any] | None:
    scripts = [node for node in project.nodes if node.kind == NodeKind.SCRIPT]
    if not scripts:
        if any(node.kind != NodeKind.ANALYSIS for node in project.nodes):
            raise ValueError("Project has an incomplete shot graph")
        return None
    if len(scripts) != 1:
        raise ValueError("Project must have one shared script node")
    graph: dict[str, Any] = {"script": scripts[0]}
    for kind in (
        NodeKind.SHOT_FRAME,
        NodeKind.SHOT_VIDEO,
        NodeKind.VOICEOVER,
        NodeKind.LIPSYNC,
        NodeKind.SUBTITLES,
    ):
        nodes = sorted(
            (node for node in project.nodes if node.kind == kind),
            key=lambda node: node.shot_index or 0,
        )
        if [node.shot_index for node in nodes] != list(range(1, count + 1)):
            raise ValueError("Changing the number of shots requires a new project")
        graph[kind.value] = nodes
    finals = [node for node in project.nodes if node.kind == NodeKind.FINAL]
    if len(finals) != 1:
        raise ValueError("Project must have one final node")
    graph["final"] = finals[0]
    return graph


def apply_storyboard(
    project: Project, storyboard: Storyboard, *, repo: ProjectRepo, cost: float = 0.0
) -> dict[str, Any]:
    """Build/update a stable shot graph without deleting any prior version."""

    storyboard = Storyboard.model_validate(storyboard)
    character, voice = _assets(project)
    graph = _existing_graph(project, len(storyboard.shots))
    with repo.session.begin_nested():
        if graph is None:
            graph = repo.create_shot_graph(project, len(storyboard.shots))
        script: Node = graph["script"]
        for analysis in (node for node in project.nodes if node.kind == NodeKind.ANALYSIS):
            if analysis not in script.depends_on:
                script.depends_on.append(analysis)
        script.recipe = storyboard.model_dump(mode="json")
        for offset, shot in enumerate(storyboard.shots):
            _update_changed(
                repo,
                graph["shot_frame"][offset],
                {
                    "visual_prompt": shot.visual_prompt,
                    "character_id": character.id,
                    "reference_images": deepcopy(character.reference_images),
                    "appearance_prompt": character.fixed_appearance_prompt,
                    "lora_path": character.lora_path,
                },
            )
            _update_changed(
                repo,
                graph["voiceover"][offset],
                {
                    "line": shot.line,
                    "emotion": shot.emotion,
                    "voice_id": voice.id,
                    "default_params": deepcopy(voice.default_params),
                    "sample_audio_paths": deepcopy(voice.sample_audio_paths),
                    "engine": voice.engine,
                    "engine_voice_id": voice.engine_voice_id,
                    "model_path": voice.model_path,
                },
            )
            _update_changed(
                repo,
                graph["shot_video"][offset],
                {
                    "camera": shot.camera,
                    "duration_s": shot.duration_s,
                },
            )
        repo.record_version(script, {}, cost=cost, note="Storyboard draft")
        repo.session.flush()
    return graph


def _update_changed(repo: ProjectRepo, node: Node, patch: dict[str, Any]) -> None:
    if any(node.recipe.get(key) != value for key, value in patch.items()):
        repo.update_recipe(node, patch)


def update_shot(
    project: Project, shot_index: int, changes: dict[str, Any], *, repo: ProjectRepo
) -> list[Node]:
    """Validate the entire edit before updating only the affected node recipes."""

    allowed = {"line", "emotion", "visual_prompt", "camera", "duration_s"}
    if not changes or set(changes) - allowed:
        raise ValueError(
            "Shot changes must contain only line/emotion/visual_prompt/camera/duration_s"
        )
    scripts = [node for node in project.nodes if node.kind == NodeKind.SCRIPT]
    if len(scripts) != 1:
        raise ValueError("Project has no unique storyboard")
    script = scripts[0]
    storyboard = Storyboard.model_validate(script.recipe)
    if shot_index not in range(1, len(storyboard.shots) + 1):
        raise ValueError(f"Shot {shot_index} does not exist")
    graph = _existing_graph(project, len(storyboard.shots))
    assert graph is not None
    proposed = storyboard.model_dump(mode="json")
    proposed["shots"][shot_index - 1].update(deepcopy(changes))
    edited = Storyboard.model_validate(proposed)
    changed_nodes: list[Node] = []
    with repo.session.begin_nested():
        for kind, fields in (
            ("voiceover", {"line", "emotion"}),
            ("shot_frame", {"visual_prompt"}),
            ("shot_video", {"camera", "duration_s"}),
        ):
            patch = {key: value for key, value in changes.items() if key in fields}
            if patch:
                node = graph[kind][shot_index - 1]
                repo.update_recipe(node, patch, note=f"Edit shot {shot_index}")
                changed_nodes.append(node)
        # Preserve the readable storyboard without propagating an unrelated full rebuild.
        script.recipe = edited.model_dump(mode="json")
        repo.record_version(script, {}, note=f"Edit shot {shot_index}")
    return changed_nodes


__all__ = [
    "Storyboard",
    "StoryboardShot",
    "ScriptAgent",
    "write_storyboard",
    "apply_storyboard",
    "update_shot",
]
