from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.agents.analysis import AnalysisPipeline, MediaInfo, Segment, ShotSpan, StructureTemplate, understand
from avd.config import ModelPrice, Settings
from avd.llm import ChatResponse, CostLedger, FakeProvider, LLMRouter
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Node, NodeKind, NodeStatus, Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def template(hook: str = "Open with an intriguing question") -> StructureTemplate:
    return StructureTemplate(
        total_duration_s=3,
        hook=hook,
        shots=[{
            "duration_s": 3,
            "shot_size": "medium",
            "camera_move": "slow push in",
            "visual_description": "A presenter centered against a plain background",
            "spoken_line_purpose": "Introduce a surprising problem",
        }],
        pacing_notes="One direct opening beat",
        music_style="Quiet ambient bed",
        voice_style="Calm explanatory delivery",
    )


def make_pipeline(
    tmp_path: Path,
    session: Session,
    responses: list[ChatResponse | str | Exception],
    *,
    has_audio: bool = True,
) -> tuple[AnalysisPipeline, Project, FakeProvider, list[str]]:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        gemini_model="fake-gemini",
        whisper_model="tiny",
        model_prices={"fake-gemini": ModelPrice(input_per_million=1)},
    )
    (tmp_path / "reference.mp4").write_bytes(b"fake video media")
    repo = ProjectRepo(session)
    project = repo.create_project("Analysis", source_video_path="reference.mp4", budget_limit=10)
    fake = FakeProvider(responses)
    router = LLMRouter(settings, providers={"gemini": fake}, ledger=CostLedger(session, settings.model_prices))
    calls: list[str] = []

    def probe(video: Path) -> MediaInfo:
        assert video == (tmp_path / "reference.mp4").resolve()
        calls.append("probe")
        return MediaInfo(duration_s=3, width=320, height=180, fps=24, has_audio=has_audio)

    def detect(video: Path) -> list[ShotSpan]:
        assert video.is_file()
        calls.append("detect")
        return [ShotSpan(start_s=0, end_s=3)]

    def extract(video: Path, output_path: Path) -> Path:
        assert video.is_file()
        calls.append("extract")
        output_path.write_bytes(b"fake wav")
        return output_path

    def transcribe(audio: Path, model_size: str) -> list[Segment]:
        assert audio.read_bytes() == b"fake wav"
        assert model_size == "tiny"
        calls.append("transcribe")
        return [Segment(start_s=0, end_s=3, text="A source sentence")]

    pipeline = AnalysisPipeline(
        repo, router, settings,
        probe_step=probe,
        detect_shots_step=detect,
        extract_audio_step=extract,
        transcribe_step=transcribe,
    )
    return pipeline, project, fake, calls


def test_understand_sends_video_and_structure_only_prompt(tmp_path: Path) -> None:
    video = tmp_path / "reference.mp4"
    video.write_bytes(b"video")
    fake = FakeProvider([template().model_dump_json()])
    router = LLMRouter(
        Settings(_env_file=None, gemini_model="fake-gemini"), providers={"gemini": fake}
    )
    result = understand(
        video,
        [ShotSpan(start_s=0, end_s=3)],
        [Segment(start_s=0, end_s=3, text="source words")],
        router=router,
    )
    assert result == template()
    messages = fake.calls[0]["messages"]
    assert "structure and style only" in messages[0]["content"]
    assert "never reproduce the verbatim line" in messages[0]["content"]
    assert messages[1]["content"][1] == {
        "type": "video", "path": str(video.resolve()), "mime_type": "video/mp4"
    }
    assert "source words" in messages[1]["content"][0]["text"]


def test_pipeline_publishes_versions_and_keeps_prior_files(tmp_path: Path, session: Session) -> None:
    first = template()
    second = template("Open with a visual contrast")
    pipeline, project, _, calls = make_pipeline(
        tmp_path, session,
        [ChatResponse(content=first.model_dump_json(), input_tokens=100), second.model_dump_json()],
    )
    assert pipeline.analyze_video(project) == first
    node = session.scalar(select(Node).where(Node.kind == NodeKind.ANALYSIS))
    assert node is not None and node.status == NodeStatus.CLEAN
    versions = pipeline.repo.list_versions(node)
    old_paths = deepcopy(versions[0].output_paths)
    old_recipe = deepcopy(versions[0].recipe_snapshot)
    old_contents = {key: (tmp_path / value).read_bytes() for key, value in old_paths.items()}
    assert versions[0].cost == pytest.approx(0.0001)
    assert old_recipe["whisper_model"] == "tiny"
    assert len(old_recipe["source_sha256"]) == 64
    assert calls == ["probe", "detect", "extract", "transcribe"]
    assert pipeline.analyze_video(project) == second
    versions = pipeline.repo.list_versions(node)
    assert [version.number for version in versions] == [1, 2]
    assert node.current_version_id == versions[1].id
    assert old_paths != versions[1].output_paths
    assert versions[0].recipe_snapshot == old_recipe
    for key, relative in old_paths.items():
        assert not Path(relative).is_absolute()
        assert (tmp_path / relative).read_bytes() == old_contents[key]
    session.commit()
    session.expire_all()
    assert project.analysis_result == second.model_dump(mode="json")
    assert json.loads((tmp_path / versions[1].output_paths["template"]).read_text()) == project.analysis_result


def test_silent_video_skips_audio_and_transcription(tmp_path: Path, session: Session) -> None:
    pipeline, project, fake, calls = make_pipeline(
        tmp_path, session, [template().model_dump_json()], has_audio=False
    )
    pipeline.analyze_video(project)
    assert calls == ["probe", "detect"]
    node = session.scalar(select(Node).where(Node.kind == NodeKind.ANALYSIS))
    assert node is not None and node.current_version is not None
    paths = node.current_version.output_paths
    assert "audio" not in paths
    assert json.loads((tmp_path / paths["transcript"]).read_text()) == []
    assert '"transcript": []' in fake.calls[0]["messages"][1]["content"][0]["text"]


def test_failed_analysis_retains_last_result(tmp_path: Path, session: Session) -> None:
    pipeline, project, _, _ = make_pipeline(
        tmp_path, session, [template().model_dump_json(), RuntimeError("provider unavailable")]
    )
    pipeline.analyze_video(project)
    node = session.scalar(select(Node).where(Node.kind == NodeKind.ANALYSIS))
    assert node is not None
    old_id = node.current_version_id
    old_recipe = deepcopy(node.recipe)
    old_result = deepcopy(project.analysis_result)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        pipeline.analyze_video(project)
    assert node.current_version_id == old_id
    assert node.recipe == old_recipe
    assert node.status == NodeStatus.CLEAN
    assert project.analysis_result == old_result
    assert len(pipeline.repo.list_versions(node)) == 1


def test_publish_failure_rolls_back_result_but_retains_paid_call(
    tmp_path: Path, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline, project, _, _ = make_pipeline(
        tmp_path, session,
        [template().model_dump_json(), ChatResponse(content=template("New hook").model_dump_json(), input_tokens=50)],
    )
    pipeline.analyze_video(project)
    node = session.scalar(select(Node).where(Node.kind == NodeKind.ANALYSIS))
    assert node is not None
    prior_version = node.current_version_id
    prior_result = deepcopy(project.analysis_result)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("storage transaction failed")

    monkeypatch.setattr(pipeline.repo, "record_version", fail)
    with pytest.raises(RuntimeError, match="storage transaction failed"):
        pipeline.analyze_video(project)
    assert node.current_version_id == prior_version
    assert node.status == NodeStatus.CLEAN
    assert project.analysis_result == prior_result
    assert project.spent == pytest.approx(0.00005)
    assert pipeline.router.ledger is not None
    assert len(pipeline.router.ledger.list(project.id)) == 2


@pytest.mark.parametrize("bad_path", [None, "../escape.mp4", "missing.mp4"])
def test_missing_and_escaping_source_fails_before_work(
    tmp_path: Path, session: Session, bad_path: str | None
) -> None:
    pipeline, project, fake, calls = make_pipeline(tmp_path, session, [])
    project.source_video_path = bad_path
    with pytest.raises((ValueError, FileNotFoundError)):
        pipeline.analyze_video(project)
    assert calls == []
    assert fake.calls == []
    assert project.analysis_result is None


def test_absolute_source_is_rejected(tmp_path: Path, session: Session) -> None:
    pipeline, project, fake, calls = make_pipeline(tmp_path, session, [])
    project.source_video_path = str((tmp_path / "reference.mp4").resolve())
    with pytest.raises(ValueError, match="relative path"):
        pipeline.analyze_video(project)
    assert calls == [] and fake.calls == []
