"""Reuse or create real stems, preserving the complete source audio timeline.

Library stems remain the original singer. Singing voice conversion is a separate
stage and may never treat these outputs as a converted voice.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Mapping

from pydantic import Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.assets.media import MediaAsset
from avd.jobs.native import run_model_process
from avd.state.paths import relative_path

from .models import PerformanceModel, PreparedFile, PreparedPerformance
from .prepare import (
    MediaTools,
    PerformanceMediaError,
    _num,
    _probe,
    _run,
    _under_root,
    _validate_audio_clock,
)
from .render import (
    AudioInput,
    _audio_input,
    _file,
    _new_folder,
    _preparation_hash,
    _write_manifest,
)

if TYPE_CHECKING:
    from avd.config import Settings


class StemSnapshot(PerformanceModel):
    """Content-addressed inputs for a separation recipe, without media probing."""

    kind: Literal["library_stems"] = "library_stems"
    source: PreparedFile
    vocals: PreparedFile
    accompaniment: PreparedFile
    asset_ids: dict[str, str]


class StemPair(PerformanceModel):
    source: PreparedFile
    vocals: AudioInput
    accompaniment: AudioInput
    asset_ids: dict[str, str] = Field(default_factory=dict)
    provenance: Literal["legacy_library_source_link", "demucs_local_model"]


class DemucsConfig(PerformanceModel):
    """Only an explicitly installed local repository is eligible for inference."""

    model_repo_relative: str
    model_name: str = Field(default="htdemucs", pattern=r"^[A-Za-z0-9_-]+$")
    device: Literal["cpu", "cuda"] = "cpu"
    shifts: int = Field(default=1, ge=0, le=10)
    overlap: float = Field(default=0.25, ge=0, le=0.99)
    timeout_s: float = Field(default=600, gt=0)

    _relative_path = field_validator("model_repo_relative")(relative_path)


class SourceAudioTimeline(PerformanceModel):
    stream_index: int = Field(ge=0)
    start_s: float
    duration_s: float = Field(gt=0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    time_base: str


class SeparationTiming(PerformanceModel):
    source_audio: SourceAudioTimeline
    source_interval_start_s: float
    source_interval_end_s: float
    stem_trim_start_s: float = Field(ge=0)
    stem_trim_end_s: float = Field(gt=0)
    leading_silence_s: float = Field(ge=0)
    trailing_silence_s: float = Field(ge=0)
    output_sample_rate: int = Field(gt=0)
    output_sample_count: int = Field(gt=0)
    output_channels: int = Field(gt=0)


class DemucsEvidence(PerformanceModel):
    config: DemucsConfig
    package_version: str
    python_version: str
    model_files: list[PreparedFile] = Field(min_length=1)
    model_sha256: str


class SeparatedPerformance(PerformanceModel):
    stage: Literal["vocal_separation"] = "vocal_separation"
    method: Literal["library", "demucs"]
    voice_converted: Literal[False] = False
    source_sha256: str
    preparation_sha256: str
    input_stems: StemPair
    timing: SeparationTiming
    vocals: AudioInput
    accompaniment: AudioInput
    ffmpeg_version: str
    demucs: DemucsEvidence | None = None


def _same_file(root: Path, expected: PreparedFile) -> None:
    actual = _file(root, expected.path)
    if actual != expected:
        raise PerformanceMediaError(f"Performance separation input changed: {expected.path}")


def _snapshot_matches(session: Session, root: Path, snapshot: StemSnapshot) -> None:
    if set(snapshot.asset_ids) != {"vocals", "accompaniment"}:
        raise PerformanceMediaError("Library stem snapshot must identify both media assets")
    for role in ("vocals", "accompaniment"):
        record = session.get(MediaAsset, snapshot.asset_ids[role])
        expected = getattr(snapshot, role)
        if (
            record is None
            or record.kind != "audio"
            or record.path != expected.path
            or record.source != snapshot.source.path
        ):
            raise PerformanceMediaError("Library stem asset changed since its recipe snapshot")
    for item in (snapshot.source, snapshot.vocals, snapshot.accompaniment):
        _same_file(root, item)


def library_stem_snapshot(
    session: Session, data_dir: Path, source_relative: str
) -> dict[str, Any] | None:
    """Find a same-content source and snapshot both library stems without FFmpeg.

    Old library rows have no recorded model version or historical source hash.
    Their database source link is retained as such, rather than claiming model
    provenance that is unavailable. The current three file hashes are immutable
    inputs to the new recipe and are rechecked at execution.
    """
    root = Path(data_dir).expanduser().resolve()
    requested = _file(root, source_relative)
    groups: dict[tuple[str, str], dict[str, MediaAsset]] = {}
    rows = session.scalars(
        select(MediaAsset)
        .where(MediaAsset.kind == "audio", MediaAsset.source.is_not(None))
        .order_by(MediaAsset.created_at.desc(), MediaAsset.id)
    )
    for record in rows:
        try:
            path = Path(relative_path(record.path))
            if path.name not in {"vocals.wav", "no_vocals.wav"} or record.source is None:
                continue
            key = (relative_path(record.source), path.parent.as_posix())
            groups.setdefault(key, {}).setdefault(path.name, record)
        except (TypeError, ValueError):
            continue
    source_files: dict[str, PreparedFile | None] = {}
    for (source, _), records in groups.items():
        if set(records) != {"vocals.wav", "no_vocals.wav"}:
            continue
        try:
            if source not in source_files:
                source_files[source] = _file(root, source)
            original = source_files[source]
            if original is None or original.sha256 != requested.sha256:
                continue
            snapshot = StemSnapshot(
                source=original,
                vocals=_file(root, records["vocals.wav"].path),
                accompaniment=_file(root, records["no_vocals.wav"].path),
                asset_ids={
                    "vocals": records["vocals.wav"].id,
                    "accompaniment": records["no_vocals.wav"].id,
                },
            )
            _same_file(root, requested)
            return snapshot.model_dump(mode="json")
        except (OSError, ValueError, PerformanceMediaError):
            source_files.setdefault(source, None)
    return None


def validate_library_snapshot(session: Session, data_dir: Path, snapshot: dict[str, Any]) -> None:
    """Check a recipe's exact source/assets/files without invoking media tools."""
    root = Path(data_dir).expanduser().resolve()
    try:
        parsed = StemSnapshot.model_validate(snapshot)
        _snapshot_matches(session, root, parsed)
    except (OSError, ValueError, TypeError) as exc:
        raise PerformanceMediaError(f"Library stem snapshot is unavailable: {exc}") from exc


def _source_timeline(
    root: Path, prepared: PreparedPerformance, tools: MediaTools
) -> SourceAudioTimeline:
    _same_file(
        root,
        PreparedFile(
            path=prepared.source.path,
            sha256=prepared.source.sha256,
            size_bytes=prepared.source.size_bytes,
        ),
    )
    audio = prepared.source.audio
    if audio is None:
        raise PerformanceMediaError("Source has no audio; select a singing reference video")
    path = root / prepared.source.path
    try:
        stream = next(
            item for item in _probe(path, tools)["streams"] if item["index"] == audio.index
        )
        if stream.get("codec_type") != "audio":
            raise ValueError("the selected source audio stream is missing")
        rate = int(stream["sample_rate"])
        time_base = str(stream["time_base"])
        if stream.get("duration_ts") not in (None, "N/A"):
            duration = float(int(stream["duration_ts"]) * Fraction(time_base))
        else:
            # Matroska commonly omits per-stream duration. Sum the actual decoded
            # samples, then reject any timestamp discontinuities below.
            frames = _probe(path, tools, frame_stream=audio.index)["frames"]
            duration = sum(int(frame["nb_samples"]) for frame in frames) / rate
        start = float(stream.get("start_time", audio.start_time_s))
        result = SourceAudioTimeline(
            stream_index=audio.index,
            start_s=start,
            duration_s=duration,
            sample_rate=rate,
            channels=int(stream["channels"]),
            time_base=time_base,
        )
        tolerance = max(1 / rate, float(Fraction(time_base)), 1e-6)
        if (
            rate != audio.sample_rate
            or result.channels != audio.channels
            or abs(start - audio.start_time_s) > tolerance
            or (audio.duration_s is not None and abs(duration - audio.duration_s) > tolerance)
        ):
            raise ValueError("source stream timing differs from preparation")
    except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as exc:
        raise PerformanceMediaError(
            f"Full source audio timeline cannot be measured: {exc}"
        ) from exc
    _validate_audio_clock(path, audio, start, start + duration, tools)
    return result


def _full_duration(stem: AudioInput, source: SourceAudioTimeline) -> None:
    tolerance = max(1 / stem.sample_rate, 1 / source.sample_rate) + 1e-9
    if abs(stem.duration_s - source.duration_s) > tolerance:
        raise PerformanceMediaError(
            f"Full source stem duration mismatch for {stem.path}: expected "
            f"{source.duration_s:.6f}s, received {stem.duration_s:.6f}s; "
            "incomplete stems cannot be padded or stretched"
        )


def _pair(
    root: Path, snapshot: StemSnapshot, source: SourceAudioTimeline, tools: MediaTools
) -> StemPair:
    vocals = _audio_input(root, snapshot.vocals.path, tools)
    accompaniment = _audio_input(root, snapshot.accompaniment.path, tools)
    for current, expected in (
        (vocals, snapshot.vocals),
        (accompaniment, snapshot.accompaniment),
    ):
        if current.sha256 != expected.sha256 or current.size_bytes != expected.size_bytes:
            raise PerformanceMediaError("Library stem changed during media probing")
        _full_duration(current, source)
    return StemPair(
        source=snapshot.source,
        vocals=vocals,
        accompaniment=accompaniment,
        asset_ids=snapshot.asset_ids,
        provenance="legacy_library_source_link",
    )


def find_library_stems(
    session: Session,
    data_dir: Path,
    prepared: PreparedPerformance,
    *,
    tools: MediaTools = MediaTools(),
    expected_snapshot: dict[str, Any] | None = None,
) -> StemPair | None:
    """Validate hashes and the *full* original audio duration before interval cuts."""
    root = Path(data_dir).expanduser().resolve()
    raw = expected_snapshot
    if raw is None:
        raw = library_stem_snapshot(session, root, prepared.source.path)
    if raw is None:
        return None
    try:
        snapshot = StemSnapshot.model_validate(raw)
    except ValidationError as exc:
        raise PerformanceMediaError("Library stem recipe snapshot is invalid") from exc
    if snapshot.source.sha256 != prepared.source.sha256:
        raise PerformanceMediaError("Library stems do not match the prepared source SHA-256")
    validate_library_snapshot(session, root, raw)
    timeline = _source_timeline(root, prepared, tools)
    result = _pair(root, snapshot, timeline, tools)
    validate_library_snapshot(session, root, raw)
    return result


def _validate_demucs_selection(
    root: Path, repo: Path, files: list[PreparedFile], name: str
) -> None:
    models: dict[str, PreparedFile] = {}
    for item in files:
        path = Path(item.path)
        if path.suffix != ".th" or (root / path).parent != repo:
            continue
        signature, separator, checksum = path.stem.rpartition("-")
        if not separator:
            signature = path.stem
        if signature in models:
            raise PerformanceMediaError(f"Duplicate Demucs model signature: {signature}")
        if separator and not item.sha256.startswith(checksum):
            raise PerformanceMediaError(f"Demucs model checksum mismatch: {path.name}")
        models[signature] = item
    if name in models:
        return
    bag_path = repo / f"{name}.yaml"
    if not bag_path.is_file():
        raise PerformanceMediaError(f"Selected Demucs model is not installed: {name}")
    try:
        bag = importlib.import_module("yaml").safe_load(bag_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PerformanceMediaError("Cannot read the selected Demucs model bag") from exc
    signatures = bag.get("models") if isinstance(bag, dict) else None
    if (
        not isinstance(signatures, list)
        or not signatures
        or any(
            not isinstance(signature, str) or signature not in models for signature in signatures
        )
    ):
        raise PerformanceMediaError("The selected Demucs model bag has missing local weights")


def _demucs_evidence(root: Path, config: DemucsConfig) -> DemucsEvidence:
    repo = _under_root(root, config.model_repo_relative)
    if not repo.is_dir():
        raise PerformanceMediaError(
            "Demucs needs an installed local model repository under data_dir"
        )
    files: list[PreparedFile] = []
    for candidate in sorted(repo.rglob("*")):
        if candidate.is_symlink() and not candidate.resolve().is_relative_to(root):
            raise PerformanceMediaError("Demucs model repository resolves outside data_dir")
        if candidate.is_file():
            files.append(_file(root, candidate.relative_to(root).as_posix()))
    if not any(Path(item.path).suffix == ".th" for item in files):
        raise PerformanceMediaError("Demucs local repository has no .th model weights")
    if config.model_name == "demucs_unittest":
        raise PerformanceMediaError("Demucs synthetic test model cannot generate real stems")
    _validate_demucs_selection(root, repo, files, config.model_name)
    try:
        version = importlib.metadata.version("demucs")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PerformanceMediaError("Install Demucs into the project Python environment") from exc
    canonical = json.dumps(
        [item.model_dump() for item in files], sort_keys=True, separators=(",", ":")
    )
    return DemucsEvidence(
        config=config,
        package_version=version,
        python_version=sys.version.split()[0],
        model_files=files,
        model_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def snapshot_demucs(data_dir: Path, config: DemucsConfig | None) -> dict[str, Any]:
    """Freeze local files without loading a model or downloading replacements."""
    snapshot: dict[str, Any] = {
        "revision": 1,
        "config": config.model_dump(mode="json") if config else None,
        "evidence": None,
        "ready": False,
        "reasons": [],
    }
    if config is None:
        snapshot["reasons"] = ["Configure a local Demucs model repository for automatic separation"]
        return snapshot
    try:
        snapshot["evidence"] = _demucs_evidence(data_dir.expanduser().resolve(), config).model_dump(
            mode="json"
        )
        snapshot["ready"] = True
    except (OSError, ValueError, RuntimeError) as exc:
        snapshot["reasons"] = [str(exc)]
    return snapshot


def validate_demucs_snapshot(data_dir: Path, snapshot: Mapping[str, Any]) -> DemucsConfig:
    """The selected runtime stays frozen across later global setting changes."""
    if not snapshot.get("ready") or not isinstance(snapshot.get("config"), dict):
        raise PerformanceMediaError(
            "Demucs was not ready when configured; install it and save the inputs again"
        )
    config = DemucsConfig.model_validate(snapshot["config"])
    if snapshot_demucs(data_dir, config) != dict(snapshot):
        raise PerformanceMediaError("Demucs files or runtime changed; save the inputs again")
    return config


def separation_readiness(
    session: Session, settings: Settings, recipe: Mapping[str, Any]
) -> dict[str, Any]:
    """Inspect the frozen library or model path without running any inference."""
    method = "library" if recipe.get("library_stems") else "demucs"
    try:
        if method == "library":
            validate_library_snapshot(session, settings.data_dir, recipe["library_stems"])
        else:
            snapshot = recipe.get("demucs")
            if not isinstance(snapshot, dict):
                raise PerformanceMediaError(
                    "No frozen separation model; configure Demucs and save the inputs again"
                )
            validate_demucs_snapshot(settings.data_dir, snapshot)
    except (OSError, ValueError, RuntimeError) as exc:
        return {"ready": False, "method": method, "reasons": [str(exc)]}
    return {"ready": True, "method": method, "reasons": []}


def _demucs_stems(
    root: Path,
    prepared: PreparedPerformance,
    source: SourceAudioTimeline,
    folder: Path,
    evidence: DemucsEvidence,
    tools: MediaTools,
) -> StemPair:
    full_audio = folder / "source_audio.wav"
    _run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-copyts",
            "-i",
            str(root / prepared.source.path),
            "-map",
            f"0:{source.stream_index}",
            "-vn",
            "-af",
            f"atrim=start={_num(source.start_s)}:end={_num(source.start_s + source.duration_s)},"
            "asetpts=PTS-STARTPTS",
            "-ar",
            str(source.sample_rate),
            "-ac",
            str(source.channels),
            "-c:a",
            "pcm_s24le",
            str(full_audio),
        ],
        tools,
    )
    _full_duration(_audio_input(root, full_audio.relative_to(root).as_posix(), tools), source)
    config = evidence.config
    arguments = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems",
        "vocals",
        "--float32",
        "--repo",
        str(root / config.model_repo_relative),
        "-n",
        config.model_name,
        "--device",
        config.device,
        "--shifts",
        str(config.shifts),
        "--overlap",
        str(config.overlap),
        "-o",
        str(folder / "demucs"),
        str(full_audio),
    ]
    environment = os.environ.copy()
    # --repo uses Demucs LocalRepo; these also stop optional integrations from
    # fetching replacement assets when the configured local repository is broken.
    environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
    bundled_tools = [
        str(Path(executable).resolve().parent)
        for executable in (tools.ffmpeg, tools.ffprobe)
        if Path(executable).is_file()
    ]
    environment["PATH"] = os.pathsep.join(
        [*dict.fromkeys(bundled_tools), environment.get("PATH", "")]
    )
    try:
        run_model_process(
            arguments,
            role="vocal_separation",
            model=config.model_name,
            phase="inference",
            details={
                "engine": "demucs",
                "package_version": evidence.package_version,
                "model_sha256": evidence.model_sha256,
                "model_files": [item.model_dump(mode="json") for item in evidence.model_files],
                "device": config.device,
            },
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=config.timeout_s,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise PerformanceMediaError("Demucs vocal separation timed out") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError):
            detail = " ".join((exc.stderr or exc.stdout or "").split())[-800:]
        raise PerformanceMediaError(f"Demucs vocal separation failed: {detail}") from exc
    produced = folder / "demucs" / config.model_name / full_audio.stem
    vocals = _audio_input(root, (produced / "vocals.wav").relative_to(root).as_posix(), tools)
    accompaniment = _audio_input(
        root, (produced / "no_vocals.wav").relative_to(root).as_posix(), tools
    )
    for stem in (vocals, accompaniment):
        _full_duration(stem, source)
    if _demucs_evidence(root, config) != evidence:
        raise PerformanceMediaError("Demucs model repository changed during separation")
    return StemPair(
        source=PreparedFile(
            path=prepared.source.path,
            sha256=prepared.source.sha256,
            size_bytes=prepared.source.size_bytes,
        ),
        vocals=vocals,
        accompaniment=accompaniment,
        provenance="demucs_local_model",
    )


def _crop_stem(
    root: Path, stem: AudioInput, destination: Path, timing: SeparationTiming, tools: MediaTools
) -> AudioInput:
    # Offset the real retained samples, then pad only the source's known missing
    # beginning/end. Full stem length was checked before this filter is allowed.
    rate = timing.output_sample_rate
    leading_samples = round(timing.leading_silence_s * rate)
    filters = (
        f"atrim=start={_num(timing.stem_trim_start_s)}:end={_num(timing.stem_trim_end_s)},"
        "asetpts=PTS-STARTPTS,"
        f"aresample={rate}:async=0,"
        f"aformat=channel_layouts={timing.output_channels}c,"
        f"adelay={leading_samples}S:all=1,"
        f"apad=whole_len={timing.output_sample_count},"
        f"atrim=end_sample={timing.output_sample_count}"
    )
    _run(
        [
            tools.ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(root / stem.path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            filters,
            "-c:a",
            "pcm_s24le",
            str(destination),
        ],
        tools,
    )
    output = _audio_input(root, destination.relative_to(root).as_posix(), tools)
    if (
        output.sample_count != timing.output_sample_count
        or output.sample_rate != rate
        or output.channels != timing.output_channels
    ):
        raise PerformanceMediaError("Cropped stem does not match the prepared sample timeline")
    return output


def separate_performance(
    data_dir: Path,
    prepared: PreparedPerformance,
    *,
    library_stems: StemPair | None = None,
    demucs: DemucsConfig | None = None,
    output_relative: str | None = None,
    tools: MediaTools = MediaTools(),
) -> SeparatedPerformance:
    """Produce aligned original stems; no voice conversion or fake fallback."""
    root = Path(data_dir).expanduser().resolve()
    source = _source_timeline(root, prepared, tools)
    canonical_duration = prepared.timeline.actual_duration_s
    tolerance = 1 / prepared.audio.sample_rate + 1e-9
    if (
        abs(prepared.audio.sample_count / prepared.audio.sample_rate - canonical_duration)
        > tolerance
        or abs(prepared.audio.duration_s - canonical_duration) > tolerance
        or abs(
            prepared.timeline.source_interval_end_pts_time_s
            - prepared.timeline.source_interval_start_pts_time_s
            - canonical_duration
        )
        > tolerance
    ):
        raise PerformanceMediaError(
            "Prepared audio sample count does not match its source interval"
        )
    evidence = None
    if library_stems is None:
        if demucs is None:
            raise PerformanceMediaError(
                "No matching complete library stems; separate the source in the asset library "
                "or configure an installed local Demucs model repository"
            )
        evidence = _demucs_evidence(root, demucs)
    else:
        if library_stems.source.sha256 != prepared.source.sha256:
            raise PerformanceMediaError("Library stems do not match the prepared source SHA-256")
        for item in (library_stems.source, library_stems.vocals, library_stems.accompaniment):
            _same_file(
                root, PreparedFile(**item.model_dump(include={"path", "sha256", "size_bytes"}))
            )
        for stem in (library_stems.vocals, library_stems.accompaniment):
            measured = _audio_input(root, stem.path, tools)
            if measured != stem:
                raise PerformanceMediaError("Library stem metadata differs from the measured WAV")
            _full_duration(measured, source)
    start = prepared.timeline.source_interval_start_pts_time_s
    end = prepared.timeline.source_interval_end_pts_time_s
    overlap_start = max(start, source.start_s)
    overlap_end = min(end, source.start_s + source.duration_s)
    if overlap_end <= overlap_start:
        raise PerformanceMediaError("Selected performance interval contains no source audio")
    timing = SeparationTiming(
        source_audio=source,
        source_interval_start_s=start,
        source_interval_end_s=end,
        stem_trim_start_s=max(0, overlap_start - source.start_s),
        stem_trim_end_s=overlap_end - source.start_s,
        leading_silence_s=max(0, source.start_s - start),
        trailing_silence_s=max(0, end - source.start_s - source.duration_s),
        output_sample_rate=prepared.audio.sample_rate,
        output_sample_count=prepared.audio.sample_count,
        output_channels=prepared.audio.channels,
    )
    folder = _new_folder(root, output_relative, "performance-stems")
    pair = library_stems
    if pair is None:
        assert evidence is not None
        pair = _demucs_stems(root, prepared, source, folder, evidence, tools)
    vocals = _crop_stem(root, pair.vocals, folder / "vocals.wav", timing, tools)
    accompaniment = _crop_stem(root, pair.accompaniment, folder / "no_vocals.wav", timing, tools)
    for item in (pair.source, pair.vocals, pair.accompaniment):
        _same_file(root, PreparedFile(**item.model_dump(include={"path", "sha256", "size_bytes"})))
    _source_timeline(root, prepared, tools)
    result = SeparatedPerformance(
        method="library" if library_stems is not None else "demucs",
        source_sha256=prepared.source.sha256,
        preparation_sha256=_preparation_hash(prepared),
        input_stems=pair,
        timing=timing,
        vocals=vocals,
        accompaniment=accompaniment,
        ffmpeg_version=_run([tools.ffmpeg, "-version"], tools).splitlines()[0],
        demucs=evidence,
    )
    _write_manifest(folder, "separation_manifest.json", result)
    return result


__all__ = [
    "DemucsConfig",
    "SeparatedPerformance",
    "StemPair",
    "StemSnapshot",
    "find_library_stems",
    "library_stem_snapshot",
    "separation_readiness",
    "separate_performance",
    "snapshot_demucs",
    "validate_demucs_snapshot",
    "validate_library_snapshot",
]
