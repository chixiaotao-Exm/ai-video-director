"""Analysis orchestration, isolated from replaceable deterministic media steps."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from avd.config import Settings
from avd.llm import CostLedger, LLMRouter
from avd.state.models import Node, NodeKind, Project
from avd.state.paths import relative_path
from avd.state.repo import ProjectRepo

from .models import MediaInfo, Segment, ShotSpan, StructureTemplate


def understand(
    video: str | Path,
    shots: list[ShotSpan],
    transcript: list[Segment],
    *,
    router: LLMRouter,
    project: Project | None = None,
) -> StructureTemplate:
    """Describe structure and style from the actual video, using the analysis role."""

    path = Path(video).resolve()
    context = {
        "shots": [shot.model_dump(mode="json") for shot in shots],
        "transcript": [segment.model_dump(mode="json") for segment in transcript],
    }
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "Analyze the reference video's structure and style only. Describe its "
                "opening hook (approximately the first 3 seconds), shot durations, shot "
                "sizes, camera movement, visual composition, pacing, music and voice "
                "style. For each spoken line describe its purpose, never reproduce the "
                "verbatim line. Do not identify or clone the person in the reference, "
                "and do not copy their face, voice identity, distinctive appearance, "
                "branding or exact creative content. The template will be used to "
                "create an original video starring separately consented assets."
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Watch this video and use these timing observations:\n"
                    + json.dumps(context, ensure_ascii=False),
                },
                {
                    "type": "video",
                    "path": str(path),
                    "mime_type": mimetypes.guess_type(path.name)[0] or "video/mp4",
                },
            ],
        },
    ]
    return router.structured("analysis", messages, StructureTemplate, project=project)


class AnalysisPipeline:
    """Run media steps and publish a new immutable result only on full success.

    Step callables are injectable so unit tests do not need optional packages,
    subprocesses, model downloads, or a live model provider. Transaction ownership
    stays with the caller, matching :class:`ProjectRepo`.
    """

    def __init__(
        self,
        repo: ProjectRepo,
        router: LLMRouter | None = None,
        settings: Settings | None = None,
        *,
        probe_step: Callable[[Path], MediaInfo] | None = None,
        detect_shots_step: Callable[[Path], list[ShotSpan]] | None = None,
        extract_audio_step: Callable[..., Path] | None = None,
        transcribe_step: Callable[..., list[Segment]] | None = None,
        understand_step: Callable[..., StructureTemplate] | None = None,
    ) -> None:
        self.repo = repo
        self.settings = settings or Settings()
        self.router = router or LLMRouter(
            self.settings,
            ledger=CostLedger(repo.session, self.settings.model_prices),
        )
        self.probe_step = probe_step
        self.detect_shots_step = detect_shots_step
        self.extract_audio_step = extract_audio_step
        self.transcribe_step = transcribe_step
        self.understand_step = understand_step or understand

    def _source(self, project: Project) -> Path:
        if not project.source_video_path:
            raise ValueError("Project has no source video")
        relative = Path(relative_path(project.source_video_path))
        root = self.settings.data_dir.expanduser().resolve()
        source = (root / relative).resolve()
        if not source.is_relative_to(root):
            raise ValueError("Source video must stay under data_dir")
        if not source.is_file():
            raise FileNotFoundError(f"Source video does not exist: {source}")
        return source

    def analyze_video(self, project: Project) -> StructureTemplate:
        """Analyze a source, retain previous versions, and update the project result."""

        from .media import extract_audio, probe
        from .steps import detect_shots, transcribe

        source = self._source(project)
        initial_spent = project.spent
        root = self.settings.data_dir.expanduser().resolve()
        run_dir = (root / "projects" / project.id / "analysis" / uuid.uuid4().hex).resolve()
        if not run_dir.is_relative_to(root):
            raise ValueError("Analysis output must stay under data_dir")
        run_dir.mkdir(parents=True, exist_ok=False)
        media = (self.probe_step or probe)(source)
        shots = (self.detect_shots_step or detect_shots)(source)
        transcript: list[Segment] = []
        outputs: dict[str, str] = {}
        if media.has_audio:
            audio = (self.extract_audio_step or extract_audio)(
                source, output_path=run_dir / "source.wav"
            )
            audio = Path(audio).resolve()
            if not audio.is_relative_to(run_dir):
                raise ValueError("Extracted audio must stay in the current analysis run")
            transcript = (self.transcribe_step or transcribe)(
                audio, model_size=self.settings.whisper_model
            )
            outputs["audio"] = audio.relative_to(root).as_posix()
        template = self.understand_step(
            source, shots, transcript, router=self.router, project=project
        )
        # Validate injected implementations too; publishing malformed results
        # would otherwise corrupt the project's durable state.
        template = StructureTemplate.model_validate(template)
        for filename, value in (
            ("template.json", template.model_dump(mode="json")),
            ("transcript.json", [segment.model_dump(mode="json") for segment in transcript]),
        ):
            destination = run_dir / filename
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
            outputs[destination.stem] = destination.relative_to(root).as_posix()

        with source.open("rb") as stream:
            source_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        route = self.router.route("analysis")
        recipe = {
            "source_video_path": project.source_video_path,
            "source_sha256": source_hash,
            "media_info": media.model_dump(mode="json"),
            "shots": [shot.model_dump(mode="json") for shot in shots],
            "whisper_model": self.settings.whisper_model,
            "provider": route.provider,
            "model": route.model,
            "template": template.model_dump(mode="json"),
        }
        # Paid calls above retain their ledger entries even if the publication
        # transaction fails. Only the final state change uses this savepoint.
        with self.repo.session.begin_nested():
            node = self.repo.session.scalar(
                select(Node).where(
                    Node.project_id == project.id,
                    Node.kind == NodeKind.ANALYSIS,
                )
            )
            if node is None:
                node = self.repo.add_node(project, NodeKind.ANALYSIS, recipe=recipe)
            else:
                self.repo.update_recipe(node, recipe)
            self.repo.record_version(
                node,
                outputs,
                cost=max(project.spent - initial_spent, 0.0),
                note="Reference structure and style analysis",
            )
            project.analysis_result = template.model_dump(mode="json")
            self.repo.session.flush()
        return template


def analyze_video(
    project: Project,
    *,
    repo: ProjectRepo,
    router: LLMRouter | None = None,
    settings: Settings | None = None,
) -> StructureTemplate:
    """Convenience entry point for a normally configured analysis pipeline."""

    return AnalysisPipeline(repo, router, settings).analyze_video(project)


__all__ = ["AnalysisPipeline", "analyze_video", "understand"]
