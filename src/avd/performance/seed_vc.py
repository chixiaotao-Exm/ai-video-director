"""Frozen local SeedVC singing conversion, separate from trained RVC checkpoints."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from avd.jobs.native import run_model_process
from avd.state.consent import require_usable
from avd.state.models import Voice
from avd.state.paths import relative_path

from .voice import (
    RVCParameters,
    SeedVCInstallation,
    SeedVCParameters,
    SingingVoiceError,
    SingingVoiceModelVersion,
    VoiceModelImport,
    VoiceModelReadiness,
    _conversion_output,
    _json_hash,
    _path,
    _sha,
    _verify_files,
    _wav_info,
    singing_execution_details,
)

_REQUIRED = {
    "Plachta/Seed-VC": {
        "config_dit_mel_seed_uvit_whisper_base_f0_44k.yml",
        "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth",
    },
    "lj1995/VoiceConversionWebUI": {"rmvpe.pt"},
    "funasr/campplus": {"campplus_cn_common.bin"},
    "nvidia/bigvgan_v2_44khz_128band_512x": {"config.json", "bigvgan_generator.pt"},
    "openai/whisper-small": {"config.json", "preprocessor_config.json", "model.safetensors"},
}


def _environment(installation: SeedVCInstallation) -> dict[str, Any]:
    executable = installation.python_executable.expanduser().resolve()
    environment = executable.parent.parent
    site_dirs = [environment / "Lib/site-packages", *environment.glob("lib/python*/site-packages")]
    packages: dict[str, Any] = {}
    for site in site_dirs:
        if not site.is_dir():
            continue
        for folder in sorted(site.glob("*.dist-info")):
            metadata = folder / "METADATA"
            if not metadata.is_file():
                continue
            text = metadata.read_text(encoding="utf-8", errors="replace")
            name = re.search(r"^Name: (.+)$", text, re.MULTILINE)
            version = re.search(r"^Version: (.+)$", text, re.MULTILINE)
            if name and version:
                key = name[1].strip().lower().replace("_", "-")
                packages[key] = {
                    "version": version[1].strip(),
                    "metadata_sha256": _sha(metadata),
                    "record_sha256": _sha(folder / "RECORD")
                    if (folder / "RECORD").is_file()
                    else None,
                }
    required = {
        "torch",
        "torchaudio",
        "transformers",
        "librosa",
        "numpy",
        "scipy",
        "soundfile",
        "descript-audio-codec",
        "descript-audiotools",
        "huggingface-hub",
        "munch",
        "einops",
        "pyyaml",
    }
    if not required.issubset(packages):
        raise SingingVoiceError(
            "The independent SeedVC Python environment is missing required packages"
        )
    return {"python_sha256": _sha(executable), "packages": packages}


def _bundle(installation: SeedVCInstallation) -> dict[str, Any]:
    manifest_path = installation.model_manifest_path.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("complete") is not True
        or manifest.get("source_commit") != installation.revision
    ):
        raise SingingVoiceError(
            "SeedVC requires a complete model manifest for the configured source revision"
        )
    models = manifest.get("models")
    if not isinstance(models, list) or {item.get("repo_id") for item in models} != set(_REQUIRED):
        raise SingingVoiceError(
            "SeedVC manifest does not contain the complete singing-model bundle"
        )
    files: list[dict[str, Any]] = []
    checkpoints = installation.project_root.expanduser().resolve() / "checkpoints"
    for model in models:
        repo = model["repo_id"]
        revision = model.get("revision", "")
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise SingingVoiceError("SeedVC model revisions must be immutable commit hashes")
        entries = model.get("files", [])
        if (
            len(entries) != len(_REQUIRED[repo])
            or {item.get("name") for item in entries} != _REQUIRED[repo]
        ):
            raise SingingVoiceError("SeedVC model files disagree with the supported singing bundle")
        for item in entries:
            path = Path(item["path"]).resolve()
            expected = item.get("sha256", "")
            if not path.is_relative_to(checkpoints) or not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise SingingVoiceError("SeedVC model paths/hashes are invalid")
            if (
                item.get("verified") is not True
                or path.stat().st_size != item["size_bytes"]
                or _sha(path) != expected
            ):
                raise SingingVoiceError("A frozen SeedVC model file is missing or changed")
            if item.get("expected_lfs_sha256") and item["expected_lfs_sha256"] != expected:
                raise SingingVoiceError(
                    "SeedVC model SHA256 differs from the official artifact manifest"
                )
            files.append(
                {
                    "repo_id": repo,
                    "revision": revision,
                    "name": item["name"],
                    "path": str(path),
                    "sha256": expected,
                    "size_bytes": item["size_bytes"],
                }
            )
    return {"manifest_sha256": _sha(manifest_path), "files": files}


def seed_installation_snapshot(installation: SeedVCInstallation | None) -> dict[str, Any]:
    """Freeze source, environment metadata and every weight without importing Torch."""
    snapshot: dict[str, Any] = {
        "engine": "seed-vc",
        "installation": installation.model_dump(mode="json") if installation else None,
        "not_ready": True,
        "reasons": [],
    }
    try:
        if installation is None:
            raise SingingVoiceError(
                "Configure seed_vc_installation before creating a SeedVC version"
            )
        root = installation.project_root.expanduser().resolve()
        if (
            not installation.python_executable.expanduser().is_file()
            or not (root / "inference.py").is_file()
        ):
            raise SingingVoiceError("Configured SeedVC Python or official inference.py is missing")
        revision = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            text=True,
            timeout=10,
        ).strip()
        if revision != installation.revision or dirty:
            raise SingingVoiceError(
                "SeedVC source revision changed or its tracked source is modified"
            )
        files = [
            root / "inference.py",
            root / "hf_utils.py",
            *sorted((root / "modules").rglob("*.py")),
        ]
        source_files = {path.relative_to(root).as_posix(): _sha(path) for path in files}
        runner = Path(__file__).with_name("seed_vc_runner.py")
        snapshot.update(
            source_files=source_files,
            runtime_source_sha256=_json_hash(source_files),
            runner_sha256=_sha(runner),
            environment=_environment(installation),
            bundle=_bundle(installation),
            not_ready=False,
        )
    except (
        SingingVoiceError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        snapshot["reasons"].append(str(error))
    return snapshot


def validate_seed_snapshot(
    installation: SeedVCInstallation | None, snapshot: dict[str, Any]
) -> None:
    if snapshot.get("not_ready") is not False:
        raise SingingVoiceError(
            "The frozen SeedVC runtime was unavailable; create a new version/recipe"
        )
    current = seed_installation_snapshot(installation)
    if current.get("not_ready") or _json_hash(current) != _json_hash(snapshot):
        raise SingingVoiceError(
            "SeedVC source, environment, weights or configuration changed; create a new version/recipe"
        )


def import_seed_voice_model(
    session: Session,
    data_dir: Path,
    request: VoiceModelImport,
    installation: SeedVCInstallation | None,
) -> SingingVoiceModelVersion:
    voice = session.get(Voice, request.voice_id)
    if voice is None:
        raise SingingVoiceError("Select an existing target voice")
    require_usable(voice)
    if request.reference_path not in voice.sample_audio_paths:
        raise SingingVoiceError("SeedVC reference_path must belong to the selected target voice")
    root = data_dir.expanduser().resolve()
    reference = _path(root, request.reference_path)
    reference_info = _wav_info(reference)
    if reference_info["duration_s"] < 1:
        raise SingingVoiceError("SeedVC requires at least one second of target reference audio")
    snapshot = seed_installation_snapshot(installation)
    if snapshot["not_ready"]:
        raise SingingVoiceError("; ".join(snapshot["reasons"]))
    reference_hash = _sha(reference)
    fingerprint = _json_hash(
        {
            "engine": "seed-vc",
            "reference_sha256": reference_hash,
            "runtime": snapshot,
            "sample_rate": 44100,
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
    if existing:
        _verify_files(root, existing)
        return existing
    folder = root / "assets/voice-models" / voice.id / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    saved_reference, saved_bundle = folder / "reference.wav", folder / "runtime.json"
    try:
        with reference.open("rb") as source, saved_reference.open("xb") as destination:
            shutil.copyfileobj(source, destination, 1024 * 1024)
        if _sha(saved_reference) != reference_hash or _sha(reference) != reference_hash:
            raise SingingVoiceError("Target reference changed during import")
        saved_bundle.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
            engine="seed-vc",
            model_path=saved_bundle.relative_to(root).as_posix(),
            model_sha256=_sha(saved_bundle),
            reference_path=saved_reference.relative_to(root).as_posix(),
            reference_sha256=reference_hash,
            runtime_snapshot=snapshot,
            sample_rate=44100,
            parameters=request.parameters.model_dump(),
            provenance=request.provenance,
            fingerprint=fingerprint,
        )
        session.add(version)
        session.flush()
        return version
    except Exception:
        saved_reference.unlink(missing_ok=True)
        saved_bundle.unlink(missing_ok=True)
        folder.rmdir()
        raise


def _run(
    arguments: list[str],
    installation: SeedVCInstallation,
    runner: Callable[..., Any] | None,
    *,
    log_path: Path | None = None,
    details: dict[str, Any] | None = None,
) -> str:
    def log(stdout: Any, stderr: Any) -> None:
        if log_path is not None:
            with log_path.open("x", encoding="utf-8") as stream:
                stream.write(str(stdout or "") + "\nSTDERR\n" + str(stderr or ""))

    try:
        execute = runner
        if execute is None:
            from functools import partial

            execute = partial(
                run_model_process,
                role="singing_voice",
                model="SeedVC",
                phase="probe" if "--probe" in arguments else "inference",
                details={
                    "engine": "seed-vc",
                    "runtime_revision": installation.revision,
                    **(details or {}),
                },
            )
        completed = execute(
            arguments,
            cwd=installation.project_root.expanduser().resolve(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=installation.timeout_s,
            shell=False,
            env=dict(
                os.environ,
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                HF_HUB_DISABLE_TELEMETRY="1",
                HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
            ),
        )
    except subprocess.TimeoutExpired as error:
        log(error.stdout, error.stderr)
        raise SingingVoiceError("SeedVC conversion/probe timed out") from error
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        log(getattr(error, "stdout", ""), getattr(error, "stderr", ""))
        detail = " ".join(str(getattr(error, "stderr", "") or error).split())[-1000:]
        raise SingingVoiceError(f"SeedVC subprocess failed: {detail}") from error
    log(completed.stdout, getattr(completed, "stderr", ""))
    return str(completed.stdout)


@contextmanager
def _seed_output(output: Path) -> Iterator[Path]:
    with _conversion_output(output) as sidecar:
        try:
            yield sidecar
        except BaseException:
            output.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            raise


def _request(snapshot: dict[str, Any], installation: SeedVCInstallation) -> dict[str, Any]:
    return {"runtime_snapshot": snapshot, "installation": installation.model_dump(mode="json")}


def seed_voice_readiness(
    session: Session,
    data_dir: Path,
    version: SingingVoiceModelVersion,
    installation: SeedVCInstallation | None,
    *,
    probe: bool = False,
    runner: Callable[..., Any] | None = None,
    parameters: SeedVCParameters | RVCParameters | None = None,
) -> VoiceModelReadiness:
    result = VoiceModelReadiness(
        version_id=version.id, engine="seed-vc", model_fingerprint=version.fingerprint
    )
    try:
        voice = session.get(Voice, version.voice_id)
        if voice is None:
            raise SingingVoiceError("The model's target voice is unavailable")
        require_usable(voice)
        root = data_dir.expanduser().resolve()
        _verify_files(root, version)
        if (
            not version.reference_path
            or not version.reference_sha256
            or not version.runtime_snapshot
        ):
            raise SingingVoiceError("SeedVC version has no frozen target reference/runtime")
        if parameters is not None and not isinstance(parameters, SeedVCParameters):
            raise SingingVoiceError("SeedVC requires SeedVC parameters")
        SeedVCParameters.model_validate(version.parameters)
        result.imported_files_valid = True
        validate_seed_snapshot(installation, version.runtime_snapshot)
        assert installation is not None
        result.runtime_files_available = True
        result.runtime_source_sha256 = version.runtime_snapshot["runtime_source_sha256"]
        result.cli_sha256 = version.runtime_snapshot["runner_sha256"]
        if not probe:
            raise SingingVoiceError("Probe the frozen SeedVC runtime before inference")
        with tempfile.TemporaryDirectory(prefix="avd-seed-probe-") as folder:
            request_path = Path(folder) / "request.json"
            request_path.write_text(
                json.dumps(_request(version.runtime_snapshot, installation)), encoding="utf-8"
            )
            stdout = _run(
                [
                    str(installation.python_executable.expanduser().resolve()),
                    str(Path(__file__).with_name("seed_vc_runner.py")),
                    "--request",
                    str(request_path),
                    "--probe",
                ],
                installation,
                runner,
                details=singing_execution_details(version),
            )
        lines = [
            line.split("=", 1)[1]
            for line in stdout.splitlines()
            if line.startswith("AVD_SEED_RUNTIME=")
        ]
        metadata = json.loads(lines[-1]) if lines else {}
        if (
            metadata.get("model_compatible") is not True
            or metadata.get("cuda_available") is not True
            or metadata.get("device") != installation.device
        ):
            raise SingingVoiceError(
                "SeedVC probe did not confirm compatible weights and CUDA availability"
            )
        result.runtime_available = result.model_compatible = result.ready_for_inference = True
        result.runtime_device = str(metadata["device"])
        result.runtime_dtype = (
            "torch.float16"
            if (parameters or SeedVCParameters.model_validate(version.parameters)).fp16
            else "torch.float32"
        )
    except (SingingVoiceError, PermissionError, ValueError, OSError, KeyError, TypeError) as error:
        result.reasons.append(str(error))
    return result


class SeedVCVoiceConverter:
    def __init__(
        self,
        data_dir: Path,
        installation: SeedVCInstallation,
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
        parameters: SeedVCParameters | None = None,
        source_version_id: str | None = None,
    ) -> dict[str, Any]:
        version = session.get(SingingVoiceModelVersion, version_id)
        if version is None or version.engine != "seed-vc":
            raise SingingVoiceError("Select a frozen SeedVC reference version")
        readiness = seed_voice_readiness(
            session,
            self.root,
            version,
            self.installation,
            probe=True,
            runner=self.runner,
            parameters=parameters,
        )
        if not readiness.ready_for_inference:
            raise SingingVoiceError("; ".join(readiness.reasons))
        source = _path(self.root, input_relative)
        output = _path(self.root, output_relative, existing=False)
        if output.suffix.lower() != ".wav":
            raise SingingVoiceError("SeedVC output must be WAV")
        original = _wav_info(source)
        input_hash = _sha(source)
        selected = parameters or SeedVCParameters.model_validate(version.parameters)
        snapshot = version.runtime_snapshot
        assert snapshot is not None and version.reference_path is not None
        with _seed_output(output) as sidecar:
            request_path = output.with_suffix(".seed-request.json")
            worker_manifest = output.with_suffix(".seed-result.json")
            payload = {
                **_request(snapshot, self.installation),
                "source": str(source),
                "source_sha256": input_hash,
                "reference": str(_path(self.root, version.reference_path)),
                "reference_sha256": version.reference_sha256,
                "output": str(output),
                "raw_output": str(output.with_suffix(".raw.wav")),
                "result": str(worker_manifest),
                "parameters": selected.model_dump(),
                "source_version_id": source_version_id,
            }
            with request_path.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            started = time.perf_counter()
            _run(
                [
                    str(self.installation.python_executable.expanduser().resolve()),
                    str(Path(__file__).with_name("seed_vc_runner.py")),
                    "--request",
                    str(request_path),
                ],
                self.installation,
                self.runner,
                log_path=output.with_suffix(".seed-stdout.log"),
                details=singing_execution_details(version),
            )
            evidence = json.loads(worker_manifest.read_text(encoding="utf-8"))
            if (
                evidence.get("status") != "succeeded"
                or evidence.get("source_sha256") != input_hash
                or evidence.get("reference_sha256") != version.reference_sha256
            ):
                raise SingingVoiceError("SeedVC returned invalid input/output provenance")
            converted = _wav_info(output)
            delta = converted["duration_s"] - original["duration_s"]
            if converted["sample_rate"] != 44100 or abs(delta) > 1 / 44100:
                raise SingingVoiceError("SeedVC normalized output changed the source timeline")
            if _sha(source) != input_hash or _sha(output) != evidence.get("normalized", {}).get(
                "sha256"
            ):
                raise SingingVoiceError("SeedVC source or output changed during conversion")
            raw_path = output.with_suffix(".raw.wav")
            if not raw_path.is_file() or _sha(raw_path) != evidence.get("raw", {}).get("sha256"):
                raise SingingVoiceError("SeedVC raw output is missing or its SHA256 changed")
            if evidence.get("parameters") != selected.model_dump() or evidence.get(
                "fixed_parameters"
            ) != {"f0_condition": True, "length_adjust": 1.0}:
                raise SingingVoiceError("SeedVC output used different frozen conversion parameters")
            _verify_files(self.root, version)
            validate_seed_snapshot(self.installation, snapshot)
            provenance = {
                "stage": "singing_voice_conversion",
                "engine": "seed-vc",
                "version_id": version.id,
                "model_fingerprint": version.fingerprint,
                "runtime_revision": self.installation.revision,
                "runtime_source_sha256": readiness.runtime_source_sha256,
                "runner_sha256": readiness.cli_sha256,
                "runtime_snapshot_hash": _json_hash(snapshot),
                "runtime_device": readiness.runtime_device,
                "runtime_dtype": readiness.runtime_dtype,
                "parameters": selected.model_dump(),
                "input": {
                    "path": relative_path(input_relative),
                    "sha256": input_hash,
                    "source_version_id": source_version_id,
                    **original,
                },
                "reference": {
                    "path": version.reference_path,
                    "sha256": version.reference_sha256,
                    "maximum_seconds_used": 25,
                },
                "output": {
                    "path": relative_path(output_relative),
                    "sha256": _sha(output),
                    **converted,
                },
                "raw": {**evidence["raw"], "path": raw_path.relative_to(self.root).as_posix()},
                "normalization": {**evidence["normalized"], "path": relative_path(output_relative)},
                "worker_manifest_path": worker_manifest.relative_to(self.root).as_posix(),
                "effective_config_sha256": evidence.get("effective_config_sha256"),
                "timing": evidence.get("timing"),
                "runtime_measurements": evidence.get("runtime_measurements"),
                "duration_delta_s": delta,
                "elapsed_s": time.perf_counter() - started,
                "quality_verified": False,
            }
            with sidecar.open("x", encoding="utf-8") as handle:
                json.dump(provenance, handle, ensure_ascii=False, indent=2)
            return {**provenance, "manifest_path": sidecar.relative_to(self.root).as_posix()}
