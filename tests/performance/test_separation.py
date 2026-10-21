"""Offline source-linked separation contracts with real FFmpeg timing checks."""

from __future__ import annotations

import array
import math
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from avd.assets.media import MediaAsset
from avd.performance import (
    MediaTools,
    PerformanceMediaError,
    PreparationRequest,
    prepare_performance,
)
from avd.performance.models import PreparedPerformance
from avd.performance.separation import (
    DemucsConfig,
    SeparatedPerformance,
    find_library_stems,
    library_stem_snapshot,
    separate_performance,
    validate_library_snapshot,
)
from avd.state.db import create_engine
from avd.state.models import Base


@pytest.fixture
def media_tools() -> MediaTools:
    bundle = Path(__file__).resolve().parents[2] / "data/tools/ffmpeg"
    ffmpeg = shutil.which("ffmpeg") or str(bundle / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(bundle / "ffprobe.exe")
    if not (Path(ffmpeg).is_file() and Path(ffprobe).is_file()):
        pytest.skip("FFmpeg and ffprobe required for real separation timing checks")
    return MediaTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


@pytest.fixture
def session() -> Any:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


def _wav(path: Path, duration: float, *, rate: int = 48000, frequency: float = 440) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = array.array("h")
    for index in range(round(rate * duration)):
        samples.append(round(0.3 * 32767 * math.sin(2 * math.pi * frequency * index / rate)))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


def _run(tools: MediaTools, args: list[str]) -> None:
    subprocess.run(
        [tools.ffmpeg, "-v", "error", "-nostdin", "-n", *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _source(
    root: Path,
    tools: MediaTools,
    *,
    audio_duration: float = 1.2,
    audio_offset: float = 0,
    start: float = 0.3,
    duration: float = 0.6,
) -> PreparedPerformance:
    _wav(root / "source_audio.wav", audio_duration)
    _run(
        tools,
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:size=96x64:rate=10:duration=1.2",
            "-itsoffset",
            str(audio_offset),
            "-i",
            str(root / "source_audio.wav"),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "pcm_s16le",
            str(root / "reference.mkv"),
        ],
    )
    return prepare_performance(
        root,
        PreparationRequest(source_video_path="reference.mkv", start_s=start, duration_s=duration),
        output_relative="prepared",
        tools=tools,
    )


def _library(
    session: Session, root: Path, *, stem_duration: float = 1.2
) -> tuple[MediaAsset, MediaAsset]:
    source = root / "uploads/original.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / "reference.mkv", source)
    vocals = "assets/media/stems/vocals.wav"
    accompaniment = "assets/media/stems/no_vocals.wav"
    _wav(root / vocals, stem_duration, rate=22050)
    _wav(root / accompaniment, stem_duration, rate=44100, frequency=110)
    rows = (
        MediaAsset(
            name="original vocals", kind="audio", path=vocals, source="uploads/original.mkv"
        ),
        MediaAsset(
            name="original music", kind="audio", path=accompaniment, source="uploads/original.mkv"
        ),
    )
    session.add_all(rows)
    session.flush()
    return rows


def _samples(root: Path, relative: str, tools: MediaTools) -> tuple[int, array.array[int]]:
    output = root / "decoded.wav"
    _run(tools, ["-i", str(root / relative), "-c:a", "pcm_s16le", str(output)])
    with wave.open(str(output), "rb") as stream:
        samples = array.array("h")
        samples.frombytes(stream.readframes(stream.getnframes()))
        return stream.getframerate(), samples


def test_library_snapshot_needs_no_ffmpeg_and_matches_an_exact_source_copy(
    tmp_path: Path, session: Session, media_tools: MediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _source(tmp_path, media_tools)
    rows = _library(session, tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("recipe snapshots must not probe media")

    with monkeypatch.context() as context:
        context.setattr("avd.performance.separation._probe", forbidden)
        snapshot = library_stem_snapshot(session, tmp_path, prepared.source.path)
    assert snapshot is not None
    assert snapshot["source"]["path"] == "uploads/original.mkv"
    assert snapshot["source"]["sha256"] == prepared.source.sha256
    assert snapshot["asset_ids"] == {"vocals": rows[0].id, "accompaniment": rows[1].id}
    stems = find_library_stems(
        session, tmp_path, prepared, tools=media_tools, expected_snapshot=snapshot
    )
    assert stems is not None and stems.provenance == "legacy_library_source_link"
    result = separate_performance(
        tmp_path, prepared, library_stems=stems, output_relative="separated", tools=media_tools
    )
    assert result.method == "library" and result.voice_converted is False
    assert result.vocals.sample_count == 28800
    assert result.vocals.sample_rate == 48000
    assert result.accompaniment.duration_s == pytest.approx(0.6)
    assert result.timing.stem_trim_start_s == pytest.approx(0.3)
    assert result.timing.stem_trim_end_s == pytest.approx(0.9)
    assert result == SeparatedPerformance.model_validate_json(
        (tmp_path / "separated/separation_manifest.json").read_text()
    )
    with pytest.raises(FileExistsError):
        separate_performance(
            tmp_path, prepared, library_stems=stems, output_relative="separated", tools=media_tools
        )


def test_no_matching_stems_has_an_actionable_error_and_does_not_create_outputs(
    tmp_path: Path, session: Session, media_tools: MediaTools
) -> None:
    prepared = _source(tmp_path, media_tools)
    assert library_stem_snapshot(session, tmp_path, prepared.source.path) is None
    assert find_library_stems(session, tmp_path, prepared, tools=media_tools) is None
    with pytest.raises(PerformanceMediaError, match="No matching complete library stems"):
        separate_performance(tmp_path, prepared, output_relative="rejected", tools=media_tools)
    assert not (tmp_path / "rejected").exists()


@pytest.mark.parametrize("change", ["delete_source", "delete_vocals", "mutate_music", "delete_row"])
def test_snapshot_validation_detects_missing_or_changed_files_without_ffmpeg(
    tmp_path: Path,
    session: Session,
    media_tools: MediaTools,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    prepared = _source(tmp_path, media_tools)
    rows = _library(session, tmp_path)
    snapshot = library_stem_snapshot(session, tmp_path, prepared.source.path)
    assert snapshot is not None
    if change == "delete_source":
        (tmp_path / "uploads/original.mkv").unlink()
    elif change == "delete_vocals":
        (tmp_path / rows[0].path).unlink()
    elif change == "mutate_music":
        _wav(tmp_path / rows[1].path, 1.2, rate=44100, frequency=220)
    else:
        session.delete(rows[0])
        session.flush()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("snapshot validation must not invoke media tools")

    monkeypatch.setattr("avd.performance.separation._probe", forbidden)
    with pytest.raises(PerformanceMediaError):
        validate_library_snapshot(session, tmp_path, snapshot)


@pytest.mark.parametrize("changed", ["source_hash", "stem_hash", "asset_path"])
def test_snapshot_refuses_replaced_inputs_without_falling_back_to_other_stems(
    tmp_path: Path, session: Session, media_tools: MediaTools, changed: str
) -> None:
    prepared = _source(tmp_path, media_tools)
    rows = _library(session, tmp_path)
    snapshot = library_stem_snapshot(session, tmp_path, prepared.source.path)
    assert snapshot is not None
    if changed == "source_hash":
        snapshot["source"]["sha256"] = "0" * 64
    elif changed == "stem_hash":
        _wav(tmp_path / rows[0].path, 1.2, frequency=880, rate=22050)
    else:
        rows[0].path = "other/vocals.wav"
        session.flush()
    with pytest.raises(PerformanceMediaError, match="match|changed"):
        find_library_stems(
            session, tmp_path, prepared, tools=media_tools, expected_snapshot=snapshot
        )


@pytest.mark.parametrize("stem_duration", [1.1, 1.3])
def test_full_source_stems_cannot_be_padded_or_truncated_to_hide_incomplete_separation(
    tmp_path: Path, session: Session, media_tools: MediaTools, stem_duration: float
) -> None:
    prepared = _source(tmp_path, media_tools)
    _library(session, tmp_path, stem_duration=stem_duration)
    with pytest.raises(PerformanceMediaError, match="Full source stem duration mismatch"):
        find_library_stems(session, tmp_path, prepared, tools=media_tools)


def test_real_source_audio_delay_and_early_end_preserve_only_known_source_silence(
    tmp_path: Path, session: Session, media_tools: MediaTools
) -> None:
    prepared = _source(
        tmp_path, media_tools, audio_duration=0.8, audio_offset=0.2, start=0, duration=1.2
    )
    _library(session, tmp_path, stem_duration=0.8)
    stems = find_library_stems(session, tmp_path, prepared, tools=media_tools)
    assert stems is not None
    result = separate_performance(tmp_path, prepared, library_stems=stems, tools=media_tools)
    assert result.timing.source_audio.duration_s == pytest.approx(0.8)
    assert result.timing.leading_silence_s == pytest.approx(0.2)
    assert result.timing.trailing_silence_s == pytest.approx(0.2)
    rate, samples = _samples(tmp_path, result.vocals.path, media_tools)
    assert max(abs(value) for value in samples[: round(0.19 * rate)]) == 0
    assert max(abs(value) for value in samples[round(1.01 * rate) :]) == 0
    assert max(abs(value) for value in samples[round(0.25 * rate) : round(0.3 * rate)]) > 8000


@pytest.mark.parametrize("changed", ["source", "stem"])
def test_source_or_stem_changed_after_finding_is_rejected_before_crop(
    tmp_path: Path, session: Session, media_tools: MediaTools, changed: str
) -> None:
    prepared = _source(tmp_path, media_tools)
    rows = _library(session, tmp_path)
    stems = find_library_stems(session, tmp_path, prepared, tools=media_tools)
    assert stems is not None
    if changed == "source":
        with (tmp_path / "reference.mkv").open("ab") as stream:
            stream.write(b"source changed")
    else:
        _wav(tmp_path / rows[0].path, 1.2, frequency=880, rate=22050)
    with pytest.raises(PerformanceMediaError, match="input changed"):
        separate_performance(
            tmp_path, prepared, library_stems=stems, output_relative="rejected", tools=media_tools
        )
    assert not (tmp_path / "rejected").exists()


def test_demucs_requires_an_explicit_local_weight_repository_before_creating_outputs(
    tmp_path: Path, media_tools: MediaTools
) -> None:
    prepared = _source(tmp_path, media_tools)
    with pytest.raises(PerformanceMediaError, match="local model repository"):
        separate_performance(
            tmp_path,
            prepared,
            demucs=DemucsConfig(model_repo_relative="models/missing"),
            output_relative="rejected",
            tools=media_tools,
        )
    assert not (tmp_path / "rejected").exists()


def test_stem_changed_during_crop_has_no_success_manifest(
    tmp_path: Path, session: Session, media_tools: MediaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    from avd.performance import separation

    prepared = _source(tmp_path, media_tools)
    rows = _library(session, tmp_path)
    stems = find_library_stems(session, tmp_path, prepared, tools=media_tools)
    assert stems is not None
    original_crop = separation._crop_stem

    def crop(*args: Any, **kwargs: Any) -> Any:
        result = original_crop(*args, **kwargs)
        if Path(args[2]).name == "vocals.wav":
            _wav(tmp_path / rows[0].path, 1.2, frequency=880, rate=22050)
        return result

    monkeypatch.setattr(separation, "_crop_stem", crop)
    with pytest.raises(PerformanceMediaError, match="input changed"):
        separate_performance(
            tmp_path, prepared, library_stems=stems, output_relative="failed", tools=media_tools
        )
    assert not (tmp_path / "failed/separation_manifest.json").exists()


def test_prepared_sample_count_cannot_pad_an_arbitrary_timeline(
    tmp_path: Path, session: Session, media_tools: MediaTools
) -> None:
    prepared = _source(tmp_path, media_tools)
    _library(session, tmp_path)
    stems = find_library_stems(session, tmp_path, prepared, tools=media_tools)
    assert stems is not None
    changed = prepared.model_copy(
        update={
            "audio": prepared.audio.model_copy(
                update={"sample_count": prepared.audio.sample_count + 200}
            )
        }
    )
    with pytest.raises(PerformanceMediaError, match="sample count"):
        separate_performance(
            tmp_path, changed, library_stems=stems, output_relative="failed", tools=media_tools
        )
    assert not (tmp_path / "failed").exists()


@pytest.mark.parametrize("failure", [None, "short", "timeout", "model_changed"])
def test_demucs_subprocess_contract_is_local_and_validates_model_outputs(
    tmp_path: Path,
    media_tools: MediaTools,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    prepared = _source(tmp_path, media_tools)
    model = tmp_path / "models/demucs/testmodel.th"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"offline test double, not a real model")
    _wav(tmp_path / "fake_vocals.wav", 1.1 if failure == "short" else 1.2)
    _wav(tmp_path / "fake_accompaniment.wav", 1.2, frequency=110)
    original_run = subprocess.run
    calls: list[list[str]] = []

    def execute(arguments: list[str], **kwargs: Any) -> Any:
        if "demucs" not in arguments:
            return original_run(arguments, **kwargs)
        calls.append(arguments)
        assert "--repo" in arguments and "--float32" in arguments
        assert kwargs["timeout"] == 7
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        assert str(Path(media_tools.ffmpeg).parent) in kwargs["env"]["PATH"]
        assert arguments[arguments.index("--repo") + 1] == str(tmp_path / "models/demucs")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 7)
        produced = Path(arguments[arguments.index("-o") + 1]) / "testmodel/source_audio"
        produced.mkdir(parents=True)
        shutil.copyfile(tmp_path / "fake_vocals.wav", produced / "vocals.wav")
        shutil.copyfile(tmp_path / "fake_accompaniment.wav", produced / "no_vocals.wav")
        if failure == "model_changed":
            model.write_bytes(b"changed model")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr("avd.performance.separation.subprocess.run", execute)
    monkeypatch.setattr(
        "avd.performance.separation.importlib.metadata.version", lambda name: "fake"
    )
    config = DemucsConfig(model_repo_relative="models/demucs", model_name="testmodel", timeout_s=7)
    if failure is not None:
        expected = {
            "short": "duration mismatch",
            "timeout": "timed out",
            "model_changed": "changed",
        }
        with pytest.raises(PerformanceMediaError, match=expected[failure]):
            separate_performance(
                tmp_path, prepared, demucs=config, output_relative="failed", tools=media_tools
            )
        assert not (tmp_path / "failed/separation_manifest.json").exists()
        return
    result = separate_performance(tmp_path, prepared, demucs=config, tools=media_tools)
    assert len(calls) == 1
    assert result.method == "demucs" and result.demucs is not None
    assert result.demucs.package_version == "fake"
    assert result.demucs.model_files[0].path == "models/demucs/testmodel.th"
    assert result.input_stems.provenance == "demucs_local_model"
    assert result.voice_converted is False
