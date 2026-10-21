"""Immutable singing models for trained RVC and zero-shot SeedVC conversion.

The supported contract is ``RVC-Project/Retrieval-based-Voice-Conversion-WebUI``
``infer/cli.py`` as documented in its ``docs/en/cli.md``. Model imports snapshot
files; SeedVC freezes its separate runtime and a registered target reference.
Importing either format does not certify conversion quality.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    func,
    inspect,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.jobs.native import run_model_process
from avd.state.consent import require_usable
from avd.state.models import Base, Voice
from avd.state.paths import relative_path


class SingingVoiceError(RuntimeError):
    """A model or conversion cannot be used without correcting its inputs/runtime."""


class VoiceSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RVCParameters(VoiceSchema):
    speaker_id: int = Field(default=0, ge=0)
    pitch: int = Field(default=0, ge=-48, le=48)
    f0_method: Literal["pm", "rmvpe"] = "rmvpe"
    index_rate: float = Field(default=0, ge=0, le=1)
    resample_sr: int = Field(default=0, ge=0, le=192000)
    rms_mix_rate: float = Field(default=0.25, ge=0, le=1)
    protect: float = Field(default=0.33, ge=0, le=0.5)

    @field_validator("resample_sr")
    @classmethod
    def valid_resampling(cls, value: int) -> int:
        if value and value < 16000:
            raise ValueError("RVC resample_sr must be zero or at least 16000 Hz")
        return value


class SeedVCParameters(VoiceSchema):
    """Zero-shot singing conversion; timing and F0 conditioning are fixed by the adapter."""

    diffusion_steps: int = Field(default=30, ge=1, le=100)
    inference_cfg_rate: float = Field(default=0.7, ge=0, le=2)
    pitch: int = Field(default=0, ge=-24, le=24)
    auto_f0_adjust: bool = False
    fp16: bool = True
    seed: int = Field(default=20261021, ge=0, le=4294967295)


class VoiceModelImport(VoiceSchema):
    voice_id: str = Field(min_length=1, max_length=36)
    engine: Literal["rvc", "seed-vc"] = "rvc"
    model_path: str | None = None
    index_path: str | None = None
    config_path: str | None = None
    reference_path: str | None = None
    sample_rate: Literal[32000, 40000, 44100, 48000]
    parameters: RVCParameters | SeedVCParameters = Field(default_factory=RVCParameters)
    provenance: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def engine_parameters(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("engine") == "seed-vc":
            value = dict(value)
            value["parameters"] = SeedVCParameters.model_validate(value.get("parameters") or {})
            value.setdefault("sample_rate", 44100)
        return value

    @field_validator("model_path", "index_path", "config_path", "reference_path")
    @classmethod
    def safe_path(cls, value: str | None) -> str | None:
        return relative_path(value) if value is not None else None

    @model_validator(mode="after")
    def index_required_when_enabled(self) -> VoiceModelImport:
        if self.engine == "seed-vc":
            if not self.reference_path:
                raise ValueError("SeedVC requires a registered target voice reference_path")
            if any((self.model_path, self.index_path, self.config_path)):
                raise ValueError("SeedVC uses the configured frozen runtime, not an RVC checkpoint")
            if self.sample_rate != 44100 or not isinstance(self.parameters, SeedVCParameters):
                raise ValueError("The SeedVC singing model requires 44100 Hz and SeedVC parameters")
            return self
        if not self.model_path or self.reference_path is not None:
            raise ValueError("RVC requires a model_path and does not accept reference_path")
        if self.sample_rate == 44100 or not isinstance(self.parameters, RVCParameters):
            raise ValueError("RVC requires its checkpoint sample rate and RVC parameters")
        if self.parameters.index_rate > 0 and self.index_path is None:
            raise ValueError("Positive index_rate requires an RVC retrieval index")
        if Path(self.model_path).suffix.lower() != ".pth":
            raise ValueError("The official RVC CLI expects a .pth checkpoint")
        if self.index_path and Path(self.index_path).suffix.lower() != ".index":
            raise ValueError("The RVC retrieval index must use the .index extension")
        return self


class SingingVoiceModelVersion(Base):
    """An immutable imported checkpoint, separate from ordinary voice/TTS samples."""

    __tablename__ = "singing_voice_model_versions"
    __table_args__ = (
        UniqueConstraint("voice_id", "number"),
        UniqueConstraint("voice_id", "fingerprint"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    voice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("voices.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="rvc")
    model_path: Mapped[str] = mapped_column(String, nullable=False)
    model_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    index_path: Mapped[str | None] = mapped_column(String, nullable=True)
    index_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_path: Mapped[str | None] = mapped_column(String, nullable=True)
    config_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_path: Mapped[str | None] = mapped_column(String, nullable=True)
    reference_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sample_rate: Mapped[int] = mapped_column(Integer, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[str] = mapped_column(String, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


def singing_execution_details(version: SingingVoiceModelVersion) -> dict[str, Any]:
    """Use the selected immutable version, never a mutable library/default name."""
    return {
        "engine": version.engine,
        "voice_model_version_id": version.id,
        "voice_model_number": version.number,
        "voice_model_fingerprint": version.fingerprint,
        "imported_artifact_sha256": version.model_sha256,
        "reference_sha256": version.reference_sha256,
    }


@event.listens_for(SingingVoiceModelVersion, "before_insert")
def _validate_model_paths(mapper: Any, connection: Any, target: SingingVoiceModelVersion) -> None:
    for key in ("model_path", "index_path", "config_path", "reference_path"):
        value = getattr(target, key)
        if value is not None:
            setattr(target, key, relative_path(value))


@event.listens_for(SingingVoiceModelVersion, "before_update")
def _immutable_model_update(mapper: Any, connection: Any, target: SingingVoiceModelVersion) -> None:
    state = inspect(target)
    if any(state.attrs[c.key].history.has_changes() for c in state.mapper.column_attrs):
        raise ValueError("Singing voice model versions are immutable; import a new version")


@event.listens_for(SingingVoiceModelVersion, "before_delete")
def _immutable_model_delete(mapper: Any, connection: Any, target: SingingVoiceModelVersion) -> None:
    raise ValueError("Singing voice model versions are immutable and cannot be deleted")


def init_voice_models(engine: Engine) -> None:
    """Register/create the additive table and SQL guards, safe to call on old databases."""
    Base.metadata.tables["singing_voice_model_versions"].create(engine, checkfirst=True)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            columns = {
                column["name"]
                for column in inspect(connection).get_columns("singing_voice_model_versions")
            }
            for name, sql_type in (
                ("reference_path", "VARCHAR"),
                ("reference_sha256", "VARCHAR(64)"),
                ("runtime_snapshot", "JSON"),
            ):
                if name not in columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE singing_voice_model_versions ADD COLUMN {name} {sql_type}"
                    )
            for action in ("UPDATE", "DELETE"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS singing_models_no_{action.lower()} "
                    f"BEFORE {action} ON singing_voice_model_versions BEGIN "
                    "SELECT RAISE(ABORT, 'Singing voice model versions are immutable'); END"
                )


def _path(root: Path, value: str, *, existing: bool = True) -> Path:
    root = root.resolve()
    result = (root / relative_path(value)).resolve()
    if not result.is_relative_to(root) or result == root:
        raise SingingVoiceError("Singing model/media paths must stay under data_dir")
    if existing and (not result.is_file() or result.stat().st_size == 0):
        raise SingingVoiceError(f"Singing model/media file is missing or empty: {value}")
    return result


def _sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def import_voice_model(
    session: Session,
    data_dir: Path,
    request: VoiceModelImport,
    *,
    seed_installation: SeedVCInstallation | None = None,
) -> SingingVoiceModelVersion:
    """Snapshot a checkpoint under data_dir without loading untrusted Python weights here."""
    if request.engine == "seed-vc":
        from .seed_vc import import_seed_voice_model

        return import_seed_voice_model(session, data_dir, request, seed_installation)
    voice = session.get(Voice, request.voice_id)
    if voice is None:
        raise SingingVoiceError("Select an existing voice before importing its singing model")
    require_usable(voice)
    root = data_dir.expanduser().resolve()
    source = {
        key: _path(root, value)
        for key in ("model_path", "index_path", "config_path")
        if (value := getattr(request, key)) is not None
    }
    hashes = {key: _sha(path) for key, path in source.items()}
    fingerprint = _json_hash(
        {
            "engine": request.engine,
            "files": hashes,
            "sample_rate": request.sample_rate,
            "parameters": request.parameters.model_dump(),
            "provenance": request.provenance,
        }
    )
    existing = session.scalar(
        select(SingingVoiceModelVersion).where(
            SingingVoiceModelVersion.voice_id == voice.id,
            SingingVoiceModelVersion.fingerprint == fingerprint,
        )
    )
    if existing is not None:
        _verify_files(root, existing)
        return existing
    folder = root / "assets" / "voice-models" / voice.id / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    copied: list[Path] = []
    paths: dict[str, str] = {}
    try:
        for key, path in source.items():
            destination = (
                folder
                / {
                    "model_path": "model.pth",
                    "index_path": "features.index",
                    "config_path": "config.json",
                }[key]
            )
            with path.open("rb") as original, destination.open("xb") as snapshot:
                copied.append(destination)
                shutil.copyfileobj(original, snapshot, 1024 * 1024)
            if _sha(destination) != hashes[key] or _sha(path) != hashes[key]:
                raise SingingVoiceError(
                    "Model files changed during import; retry with stable files"
                )
            paths[key] = destination.relative_to(root).as_posix()
        number = (
            session.scalar(
                select(func.max(SingingVoiceModelVersion.number)).where(
                    SingingVoiceModelVersion.voice_id == voice.id
                )
            )
            or 0
        ) + 1
        version = SingingVoiceModelVersion(
            voice_id=voice.id,
            number=number,
            engine=request.engine,
            model_path=paths["model_path"],
            model_sha256=hashes["model_path"],
            index_path=paths.get("index_path"),
            index_sha256=hashes.get("index_path"),
            config_path=paths.get("config_path"),
            config_sha256=hashes.get("config_path"),
            sample_rate=request.sample_rate,
            parameters=request.parameters.model_dump(),
            provenance=request.provenance,
            fingerprint=fingerprint,
        )
        session.add(version)
        session.flush()
        return version
    except Exception:
        for path in copied:
            path.unlink(missing_ok=True)
        folder.rmdir()
        raise


def _verify_files(root: Path, version: SingingVoiceModelVersion) -> None:
    for field in ("model", "index", "config", "reference"):
        value = getattr(version, f"{field}_path")
        expected = getattr(version, f"{field}_sha256")
        if value is not None and _sha(_path(root, value)) != expected:
            raise SingingVoiceError(f"Imported {field} changed; restore it or import a new version")


class RVCInstallation(VoiceSchema):
    project_root: Path
    python_executable: Path
    revision: str = Field(min_length=1)
    # The official CLI selects its device through Config; it has no --device flag.
    # This optional expectation prevents accepting an unwanted automatic fallback.
    device: str | None = Field(default=None, min_length=1)
    timeout_s: float = Field(default=600, gt=0)
    duration_tolerance_s: float = Field(default=0.05, gt=0, le=0.25)
    cli_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    def cli(self) -> Path:
        return self.project_root.expanduser().resolve() / "infer" / "cli.py"


class SeedVCInstallation(VoiceSchema):
    project_root: Path
    python_executable: Path
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    model_manifest_path: Path
    device: Literal["cuda"] = "cuda"
    timeout_s: float = Field(default=600, gt=0)
    duration_tolerance_s: float = Field(default=0.01, gt=0, le=0.01)


class VoiceModelReadiness(VoiceSchema):
    version_id: str
    probe_job_id: str | None = None
    probe_status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None
    imported_files_valid: bool = False
    runtime_files_available: bool = False
    runtime_available: bool = False
    model_compatible: bool = False
    ready_for_inference: bool = False
    quality_verified: Literal[False] = False
    reasons: list[str] = Field(default_factory=list)
    model_fingerprint: str | None = None
    cli_sha256: str | None = None
    speaker_ids: list[int] = Field(default_factory=list)
    runtime_device: str | None = None
    runtime_dtype: str | None = None
    runtime_source_sha256: str | None = None
    engine: str | None = None


def _run(
    arguments: list[str],
    installation: RVCInstallation,
    runner: Callable[..., Any] | None,
    *,
    details: dict[str, Any] | None = None,
) -> str:
    try:
        execute = runner
        if execute is None:
            from functools import partial

            execute = partial(
                run_model_process,
                role="singing_voice",
                model="RVC",
                phase="probe"
                if "--list-speakers" in arguments or "-c" in arguments
                else "inference",
                details={
                    "engine": "rvc",
                    "runtime_revision": installation.revision,
                    **(details or {}),
                },
            )
        result = execute(
            arguments,
            cwd=installation.project_root.expanduser().resolve(),
            capture_output=True,
            text=True,
            check=True,
            timeout=installation.timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SingingVoiceError("RVC conversion/probe timed out") from exc
    except FileNotFoundError as exc:
        raise SingingVoiceError("Configured RVC Python executable is unavailable") from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join(str(exc.stderr or "").split())[-500:]
        raise SingingVoiceError(f"RVC CLI failed: {detail}") from exc
    return str(result.stdout)


def _speaker_ids(stdout: str) -> list[int]:
    named: list[int] = []
    for line in stdout.splitlines():
        interval = re.fullmatch(r"(\d+)-(\d+)", line.strip())
        if interval:
            start, end = map(int, interval.groups())
            if start > end or end - start > 4096:
                raise SingingVoiceError("RVC returned an invalid speaker range")
            return list(range(start, end + 1))
        speaker = re.fullmatch(r"(\d+)\t.+", line.strip())
        if speaker:
            named.append(int(speaker[1]))
    if named:
        return sorted(set(named))
    raise SingingVoiceError("RVC --list-speakers returned no checkpoint metadata")


def _runtime_metadata(
    installation: RVCInstallation,
    runner: Callable[..., Any] | None,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    script = (
        "import json; from configs.config import Config; c=Config(); "
        "print('AVD_RVC_RUNTIME='+json.dumps({'device':str(c.device),'dtype':str(c.dtype)}))"
    )
    stdout = _run(
        [str(installation.python_executable.expanduser().resolve()), "-c", script],
        installation,
        runner,
        details=details,
    )
    for line in stdout.splitlines():
        if line.startswith("AVD_RVC_RUNTIME="):
            result = json.loads(line.split("=", 1)[1])
            if (
                isinstance(result, dict)
                and isinstance(result.get("device"), str)
                and isinstance(result.get("dtype"), str)
            ):
                return result
    raise SingingVoiceError("RVC Config probe returned no device/precision metadata")


def _runtime_hash(installation: RVCInstallation) -> str:
    root = installation.project_root.expanduser().resolve()
    files = sorted(
        {
            path
            for folder in (root / "infer", root / "configs")
            for path in folder.rglob("*.py")
            if path.is_file()
        }
    )
    return _json_hash({path.relative_to(root).as_posix(): _sha(path) for path in files})


def rvc_installation_snapshot(installation: RVCInstallation | None) -> dict[str, Any]:
    """Freeze runtime configuration and source files without loading a model or subprocess."""
    snapshot: dict[str, Any] = {
        "installation": installation.model_dump(mode="json") if installation else None,
        "cli_sha256": None,
        "runtime_source_sha256": None,
        "not_ready": True,
        "reasons": [],
    }
    reasons: list[str] = snapshot["reasons"]
    if installation is None:
        reasons.append("Configure the official RVC CLI runtime before conversion")
        return snapshot
    try:
        if not installation.python_executable.expanduser().is_file():
            reasons.append("Configured RVC Python executable is missing")
        if not installation.cli().is_file():
            reasons.append("Configured RVC infer/cli.py is missing")
        else:
            snapshot["cli_sha256"] = _sha(installation.cli())
            snapshot["runtime_source_sha256"] = _runtime_hash(installation)
            if installation.cli_sha256 and snapshot["cli_sha256"] != installation.cli_sha256:
                reasons.append("RVC CLI changed; review the configured runtime version")
    except OSError:
        reasons.append("Configured RVC runtime files cannot be read")
    snapshot["not_ready"] = bool(reasons)
    return snapshot


def validate_rvc_snapshot(installation: RVCInstallation | None, snapshot: dict[str, Any]) -> None:
    """Reject a changed or originally unavailable runtime before using a frozen recipe."""
    if snapshot.get("not_ready") is not False:
        raise SingingVoiceError("The frozen RVC runtime was not ready; create a new recipe")
    current = rvc_installation_snapshot(installation)
    if snapshot.get("installation") != current["installation"]:
        raise SingingVoiceError("RVC runtime configuration changed; create a new recipe")
    if current["not_ready"]:
        raise SingingVoiceError("The frozen RVC runtime is unavailable or changed")
    for field in ("cli_sha256", "runtime_source_sha256"):
        expected = snapshot.get(field)
        if not isinstance(expected, str) or expected != current[field]:
            raise SingingVoiceError("RVC runtime source files changed; create a new recipe")


def voice_model_readiness(
    session: Session,
    data_dir: Path,
    version_id: str,
    *,
    installation: RVCInstallation | None = None,
    seed_installation: SeedVCInstallation | None = None,
    probe: bool = False,
    runner: Callable[..., Any] | None = None,
    parameters: RVCParameters | SeedVCParameters | None = None,
) -> VoiceModelReadiness:
    result = VoiceModelReadiness(version_id=version_id)
    version = session.get(SingingVoiceModelVersion, version_id)
    if version is None:
        result.reasons.append(
            "Import a singing voice model; ordinary voice samples are not RVC models"
        )
        return result
    if version.engine == "seed-vc":
        from .seed_vc import seed_voice_readiness

        return seed_voice_readiness(
            session,
            data_dir,
            version,
            seed_installation,
            probe=probe,
            runner=runner,
            parameters=parameters,
        )
    result.engine = version.engine
    root = data_dir.expanduser().resolve()
    result.model_fingerprint = version.fingerprint
    try:
        voice = session.get(Voice, version.voice_id)
        if voice is None:
            raise SingingVoiceError("The model's target voice is unavailable")
        require_usable(voice)
        if version.engine != "rvc":
            raise SingingVoiceError("The selected singing model is not an RVC checkpoint")
        _verify_files(root, version)
        result.imported_files_valid = True
        if installation is None:
            raise SingingVoiceError("Configure the official RVC CLI runtime before conversion")
        if (
            not installation.python_executable.expanduser().is_file()
            or not installation.cli().is_file()
        ):
            raise SingingVoiceError("Configured RVC Python or infer/cli.py is missing")
        result.cli_sha256 = _sha(installation.cli())
        if installation.cli_sha256 and result.cli_sha256 != installation.cli_sha256:
            raise SingingVoiceError(
                "RVC CLI changed; review and update the configured runtime version"
            )
        result.runtime_files_available = True
        if not probe:
            raise SingingVoiceError("Probe the imported model with RVC before inference")
        details = singing_execution_details(version)
        runtime = _runtime_metadata(installation, runner, details=details)
        result.runtime_device, result.runtime_dtype = runtime["device"], runtime["dtype"]
        if (
            installation.device
            and runtime["device"] != installation.device
            and not runtime["device"].startswith(installation.device + ":")
        ):
            raise SingingVoiceError(
                "RVC selected a different device than required; configure the runtime before inference"
            )
        speakers = _speaker_ids(
            _run(
                [
                    str(installation.python_executable.expanduser().resolve()),
                    str(installation.cli()),
                    "--model",
                    str(_path(root, version.model_path)),
                    "--list-speakers",
                ],
                installation,
                runner,
                details=details,
            )
        )
        result.runtime_available = True
        selected = parameters or RVCParameters.model_validate(version.parameters)
        if not isinstance(selected, RVCParameters):
            raise SingingVoiceError("RVC requires RVC parameters")
        if selected.speaker_id not in speakers:
            raise SingingVoiceError("Configured speaker_id is not present in the RVC checkpoint")
        result.model_compatible = True
        result.speaker_ids = speakers
        result.runtime_source_sha256 = _runtime_hash(installation)
        result.ready_for_inference = True
    except (SingingVoiceError, PermissionError, ValueError, OSError) as exc:
        result.reasons.append(str(exc))
    return result


def _wav_info(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as audio:
            result: dict[str, Any] = {
                "sample_rate": audio.getframerate(),
                "sample_count": audio.getnframes(),
                "channels": audio.getnchannels(),
            }
    except (wave.Error, EOFError, OSError) as exc:
        raise SingingVoiceError("Singing input/output must be readable PCM WAV audio") from exc
    if result["sample_count"] <= 0 or result["sample_rate"] <= 0:
        raise SingingVoiceError("Singing input/output audio is empty")
    result["duration_s"] = result["sample_count"] / result["sample_rate"]
    return result


@contextmanager
def _conversion_output(output: Path) -> Iterator[Path]:
    """Only the owner of a destination may create or clean its conversion files."""
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_suffix(".conversion.lock")
    sidecar = output.with_suffix(".conversion.json")
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise FileExistsError(
            "A singing conversion is already in progress for this output"
        ) from exc
    try:
        with handle:
            handle.write("Singing conversion in progress\n")
        if output.exists() or sidecar.exists():
            raise FileExistsError("Refusing to overwrite a singing conversion or its provenance")
        yield sidecar
    finally:
        lock.unlink(missing_ok=True)


class RVCVoiceConverter:
    def __init__(
        self,
        data_dir: Path,
        installation: RVCInstallation,
        *,
        runner: Callable[..., Any] | None = None,
    ):
        self.root = data_dir.expanduser().resolve()
        self.installation = installation
        self.runner = runner

    def convert(
        self,
        session: Session,
        version_id: str,
        input_relative: str,
        output_relative: str,
        *,
        parameters: RVCParameters | None = None,
    ) -> dict[str, Any]:
        runtime_snapshot = rvc_installation_snapshot(self.installation)
        readiness = voice_model_readiness(
            session,
            self.root,
            version_id,
            installation=self.installation,
            probe=True,
            runner=self.runner,
            parameters=parameters,
        )
        if not readiness.ready_for_inference:
            raise SingingVoiceError("; ".join(readiness.reasons))
        validate_rvc_snapshot(self.installation, runtime_snapshot)
        version = session.get(SingingVoiceModelVersion, version_id)
        assert version is not None
        settings = parameters or RVCParameters.model_validate(version.parameters)
        if settings.speaker_id not in readiness.speaker_ids:
            raise SingingVoiceError("Configured speaker_id is not present in the RVC checkpoint")
        if settings.index_rate > 0 and version.index_path is None:
            raise SingingVoiceError("Positive index_rate requires an imported RVC retrieval index")
        source = _path(self.root, input_relative)
        output = _path(self.root, output_relative, existing=False)
        if output.suffix.lower() != ".wav":
            raise SingingVoiceError("Singing conversion output must be WAV")
        sidecar = output.with_suffix(".conversion.json")
        if output.exists() or sidecar.exists():
            raise FileExistsError("Refusing to overwrite a singing conversion or its provenance")
        original = _wav_info(source)
        source_hash = _sha(source)
        output.parent.mkdir(parents=True, exist_ok=True)
        args = [
            str(self.installation.python_executable.expanduser().resolve()),
            str(self.installation.cli()),
            "--model",
            str(_path(self.root, version.model_path)),
            "--input",
            str(source),
            "--output",
            str(output),
            "--format",
            "wav",
        ]
        for name, value in settings.model_dump().items():
            args.extend(["--" + name.replace("_", "-"), str(value)])
        if version.index_path:
            args.extend(["--index", str(_path(self.root, version.index_path))])
        with _conversion_output(output) as sidecar:
            sidecar_created = False
            try:
                started = time.perf_counter()
                _run(
                    args, self.installation, self.runner, details=singing_execution_details(version)
                )
                elapsed_s = time.perf_counter() - started
                converted = _wav_info(output)
                expected_rate = settings.resample_sr or version.sample_rate
                if converted["sample_rate"] != expected_rate:
                    raise SingingVoiceError(
                        "RVC output sample rate disagrees with the requested/model rate"
                    )
                duration_delta = Fraction(
                    converted["sample_count"], converted["sample_rate"]
                ) - Fraction(original["sample_count"], original["sample_rate"])
                tolerance = min(
                    Fraction(str(self.installation.duration_tolerance_s)),
                    max(
                        Fraction(1, original["sample_rate"]),
                        Fraction(1, converted["sample_rate"]),
                    ),
                )
                if abs(duration_delta) > tolerance:
                    raise SingingVoiceError(
                        "RVC changed the singing duration; inspect the model/output instead of stretching"
                    )
                if _sha(source) != source_hash:
                    raise SingingVoiceError("Singing input changed during conversion")
                _verify_files(self.root, version)
                validate_rvc_snapshot(self.installation, runtime_snapshot)
                provenance = {
                    "stage": "singing_voice_conversion",
                    "engine": "rvc",
                    "version_id": version.id,
                    "model_fingerprint": version.fingerprint,
                    "runtime_revision": self.installation.revision,
                    "cli_sha256": readiness.cli_sha256,
                    "runtime_source_sha256": readiness.runtime_source_sha256,
                    "runtime_device": readiness.runtime_device,
                    "runtime_dtype": readiness.runtime_dtype,
                    "parameters": settings.model_dump(),
                    "input": {
                        "path": relative_path(input_relative),
                        "sha256": source_hash,
                        **original,
                    },
                    "output": {
                        "path": relative_path(output_relative),
                        "sha256": _sha(output),
                        **converted,
                    },
                    "duration_delta_s": float(duration_delta),
                    "duration_tolerance_s": float(tolerance),
                    "elapsed_s": elapsed_s,
                    "quality_verified": False,
                }
                provenance["configuration_hash"] = _json_hash(
                    {
                        key: provenance[key]
                        for key in (
                            "model_fingerprint",
                            "runtime_revision",
                            "cli_sha256",
                            "runtime_source_sha256",
                            "runtime_device",
                            "runtime_dtype",
                            "parameters",
                            "duration_tolerance_s",
                        )
                    }
                )
                with sidecar.open("x", encoding="utf-8") as handle:
                    sidecar_created = True
                    json.dump(provenance, handle, ensure_ascii=False, indent=2)
                return {**provenance, "manifest_path": sidecar.relative_to(self.root).as_posix()}
            except Exception:
                output.unlink(missing_ok=True)
                if sidecar_created:
                    sidecar.unlink(missing_ok=True)
                raise


__all__ = [
    "RVCInstallation",
    "RVCParameters",
    "RVCVoiceConverter",
    "SeedVCInstallation",
    "SeedVCParameters",
    "SingingVoiceError",
    "SingingVoiceModelVersion",
    "VoiceModelImport",
    "VoiceModelReadiness",
    "import_voice_model",
    "init_voice_models",
    "rvc_installation_snapshot",
    "validate_rvc_snapshot",
    "voice_model_readiness",
    "list_voice_reference_samples",
]


def list_voice_reference_samples(session: Session, data_dir: Path, voice_id: str) -> dict[str, Any]:
    """List registered files for one voice without enrolling or copying other assets."""
    voice = session.get(Voice, voice_id)
    if voice is None:
        raise SingingVoiceError("The target voice does not exist")
    samples: list[dict[str, Any]] = []
    root = data_dir.expanduser().resolve()
    for relative in voice.sample_audio_paths:
        item: dict[str, Any] = {"path": relative_path(relative), "exists": False}
        try:
            path = _path(root, relative)
            item.update(exists=True, sha256=_sha(path))
            try:
                item.update(_wav_info(path))
            except SingingVoiceError:
                item["metadata_available"] = False
        except (SingingVoiceError, ValueError, OSError) as error:
            item["reason"] = str(error)
        samples.append(item)
    return {"voice_id": voice.id, "reference_samples": samples}
