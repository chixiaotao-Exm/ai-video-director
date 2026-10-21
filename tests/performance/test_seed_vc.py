"""Offline SeedVC lifecycle, runtime drift and audio timeline rejection checks."""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from avd.jobs.models import Job
from avd.performance import seed_vc
from avd.performance.probe_state import persisted_voice_readiness
from avd.performance.seed_vc import SeedVCVoiceConverter
from avd.performance.seed_vc_runner import normalize_audio
from avd.performance.voice import (
    SeedVCInstallation,
    SeedVCParameters,
    SingingVoiceError,
    VoiceModelImport,
    _sha,
    import_voice_model,
    init_voice_models,
    list_voice_reference_samples,
    voice_model_readiness,
)
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Voice


def wav(path: Path, samples: int = 44100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b"\x10\x00" * samples)


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime = tmp_path / "seed-runtime"
    runtime.mkdir()
    (runtime / "inference.py").write_text("# fake official entry", encoding="utf-8")
    (runtime / "hf_utils.py").write_text("# fake local helper", encoding="utf-8")
    (runtime / "modules").mkdir()
    (runtime / "modules/model.py").write_text("# fake source", encoding="utf-8")
    models = []
    for repo, names in seed_vc._REQUIRED.items():
        files = []
        for name in sorted(names):
            path = runtime / "checkpoints" / repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"OFFLINE MODEL FIXTURE: " + name.encode())
            files.append(
                {
                    "name": name,
                    "path": str(path),
                    "sha256": _sha(path),
                    "size_bytes": path.stat().st_size,
                    "verified": True,
                }
            )
        models.append({"repo_id": repo, "revision": "b" * 40, "files": files})
    manifest = tmp_path / "models.json"
    manifest.write_text(
        json.dumps({"complete": True, "source_commit": "a" * 40, "models": models}),
        encoding="utf-8",
    )
    installation = SeedVCInstallation(
        project_root=runtime,
        python_executable=Path(sys.executable),
        revision="a" * 40,
        model_manifest_path=manifest,
    )
    monkeypatch.setattr(seed_vc, "_environment", lambda _: {"packages": {"offline": "1"}})
    monkeypatch.setattr(
        seed_vc.subprocess,
        "check_output",
        lambda args, **kwargs: "a" * 40 if "rev-parse" in args else "",
    )
    engine = create_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    init_db(engine)
    init_voice_models(engine)
    factory = create_session_factory(engine)
    wav(tmp_path / "sample.wav", 88200)
    wav(tmp_path / "source.wav")
    with factory() as session:
        voice = Voice(
            name="Consented sample fixture",
            consent_confirmed=True,
            sample_audio_paths=["sample.wav"],
        )
        session.add(voice)
        session.flush()
        request = VoiceModelImport(
            voice_id=voice.id,
            engine="seed-vc",
            reference_path="sample.wav",
            provenance="Offline SeedVC fixture",
        )
        version = import_voice_model(session, tmp_path, request, seed_installation=installation)
        session.commit()
        yield tmp_path, session, voice, request, version, installation
    engine.dispose()


def fake_runner(*, samples: int = 44100, failure: str | None = None):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert kwargs["shell"] is False and kwargs["timeout"] == 600
        assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
        request = json.loads(
            Path(arguments[arguments.index("--request") + 1]).read_text(encoding="utf-8")
        )
        if "--probe" in arguments:
            return SimpleNamespace(
                stdout='AVD_SEED_RUNTIME={"model_compatible":true,"cuda_available":true,"device":"cuda"}',
                stderr="",
            )
        output, raw = Path(request["output"]), Path(request["raw_output"])
        wav(output, samples)
        raw.write_bytes(b"OFFLINE RAW FLOAT FIXTURE")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                arguments, 600, output="partial worker log", stderr="timeout diagnostic"
            )
        result = {
            "status": "succeeded",
            "source_sha256": request["source_sha256"],
            "reference_sha256": request["reference_sha256"],
            "parameters": request["parameters"],
            "fixed_parameters": {"f0_condition": True, "length_adjust": 1.0},
            "normalized": {"sha256": _sha(output)},
            "raw": {"sha256": _sha(raw)},
        }
        if failure == "hash":
            result["normalized"]["sha256"] = "0" * 64
        if failure == "raw":
            raw.unlink()
        Path(request["result"]).write_text(json.dumps(result), encoding="utf-8")
        return SimpleNamespace(stdout="OFFLINE successful conversion", stderr="")

    return run, calls


def test_seed_version_freezes_reference_and_preserves_rvc_separation(seeded):
    root, session, _, request, version, installation = seeded
    assert version.engine == "seed-vc" and version.sample_rate == 44100
    assert isinstance(request.parameters, SeedVCParameters)
    assert (root / version.reference_path).read_bytes() == (root / "sample.wav").read_bytes()
    assert (
        import_voice_model(session, root, request, seed_installation=installation).id == version.id
    )
    assert version.runtime_snapshot["not_ready"] is False
    (root / "sample.wav").write_bytes(b"original registration changed after import")
    assert (root / version.reference_path).stat().st_size > 44100


def test_seed_reference_must_belong_to_selected_voice(seeded):
    root, session, _, request, _, installation = seeded
    wav(root / "other.wav")
    with pytest.raises(SingingVoiceError, match="belong"):
        import_voice_model(
            session,
            root,
            request.model_copy(update={"reference_path": "other.wav"}),
            seed_installation=installation,
        )


def test_seed_import_requires_target_consent(seeded):
    root, session, voice, request, _, installation = seeded
    voice.consent_confirmed = False
    with pytest.raises(PermissionError):
        import_voice_model(session, root, request, seed_installation=installation)


def test_seed_import_requires_one_second_reference(seeded):
    root, session, _, request, _, installation = seeded
    wav(root / "sample.wav", 100)
    with pytest.raises(SingingVoiceError, match="one second"):
        import_voice_model(session, root, request, seed_installation=installation)


@pytest.mark.parametrize(
    "field,value",
    [("model_path", "fake.pth"), ("sample_rate", 48000), ("parameters", {"protect": 0.1})],
)
def test_seed_contract_rejects_rvc_parameters_and_checkpoint(field, value):
    payload = {
        "voice_id": "fixture",
        "engine": "seed-vc",
        "reference_path": "voice.wav",
        "provenance": "fixture",
        field: value,
    }
    with pytest.raises(ValidationError):
        VoiceModelImport.model_validate(payload)


@pytest.mark.parametrize("change", ["source", "weight", "manifest", "reference"])
def test_seed_readiness_rejects_frozen_file_drift(seeded, change):
    root, session, _, _, version, installation = seeded
    if change == "source":
        path = installation.project_root / "modules/model.py"
    elif change == "weight":
        path = Path(version.runtime_snapshot["bundle"]["files"][0]["path"])
    elif change == "manifest":
        path = installation.model_manifest_path
    else:
        path = root / version.reference_path
    path.write_bytes(path.read_bytes() + b"changed")
    run, calls = fake_runner()
    result = voice_model_readiness(
        session, root, version.id, seed_installation=installation, probe=True, runner=run
    )
    assert not result.ready_for_inference and result.reasons and not calls


def test_seed_readiness_probe_is_explicit_and_never_certifies_quality(seeded):
    root, session, _, _, version, installation = seeded
    run, calls = fake_runner()
    result = voice_model_readiness(
        session, root, version.id, seed_installation=installation, runner=run
    )
    assert result.runtime_files_available and not result.ready_for_inference and not calls
    result = voice_model_readiness(
        session, root, version.id, seed_installation=installation, probe=True, runner=run
    )
    assert result.ready_for_inference and result.model_compatible and result.engine == "seed-vc"
    assert not result.quality_verified and len(calls) == 1


@pytest.mark.parametrize("drift", [None, "source", "reference", "environment"])
def test_seed_persisted_probe_requires_current_frozen_runtime(seeded, monkeypatch, drift):
    root, session, _, _, version, installation = seeded
    run, calls = fake_runner()
    ready = voice_model_readiness(
        session, root, version.id, seed_installation=installation, probe=True, runner=run
    )
    session.add(
        Job(
            lane="gpu",
            job_type="singing_model_probe",
            status="succeeded",
            payload={
                "version_id": version.id,
                "seed_vc_installation": installation.model_dump(mode="json"),
            },
            result={"readiness": ready.model_dump(mode="json")},
        )
    )
    session.commit()
    if drift == "source":
        (installation.project_root / "modules/model.py").write_text("# source changed")
    elif drift == "reference":
        (root / version.reference_path).write_bytes(b"changed target reference")
    elif drift == "environment":
        monkeypatch.setattr(seed_vc, "_environment", lambda _: {"packages": {"offline": "2"}})
    restored = persisted_voice_readiness(session, root, version.id, seed_installation=installation)
    assert restored.ready_for_inference is (drift is None)
    assert len(calls) == 1 and not restored.quality_verified


def test_seed_converter_records_timeline_hashes_and_source_version(seeded):
    root, session, _, _, version, installation = seeded
    run, calls = fake_runner()
    result = SeedVCVoiceConverter(root, installation, runner=run).convert(
        session,
        version.id,
        "source.wav",
        "result/converted.wav",
        source_version_id="separation-version-fixture",
    )
    assert result["output"]["sample_count"] == 44100
    assert result["duration_delta_s"] == 0 and not result["quality_verified"]
    assert result["input"]["source_version_id"] == "separation-version-fixture"
    assert result["raw"]["path"] == "result/converted.raw.wav"
    assert len(calls) == 2
    assert (root / result["manifest_path"]).is_file()
    assert not (root / "result/converted.conversion.lock").exists()
    with pytest.raises(FileExistsError):
        SeedVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "source.wav", "result/converted.wav"
        )


@pytest.mark.parametrize("failure", ["timeline", "hash", "raw", "timeout"])
def test_seed_converter_rejects_invalid_output_and_keeps_failure_evidence(seeded, failure):
    root, session, _, _, version, installation = seeded
    run, _ = fake_runner(samples=44000 if failure == "timeline" else 44100, failure=failure)
    with pytest.raises(SingingVoiceError):
        SeedVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "source.wav", "bad/output.wav"
        )
    assert not (root / "bad/output.wav").exists()
    assert not (root / "bad/output.conversion.json").exists()
    assert (root / "bad/output.seed-stdout.log").exists()
    assert (root / "bad/output.seed-request.json").exists()


def test_reference_catalog_only_lists_registered_samples(seeded):
    root, session, voice, _, _, _ = seeded
    wav(root / "not-registered.wav")
    samples = list_voice_reference_samples(session, root, voice.id)["reference_samples"]
    assert len(samples) == 1 and samples[0]["path"] == "sample.wav"
    assert samples[0]["sample_count"] == 88200 and samples[0]["sha256"] == _sha(root / "sample.wav")


def test_old_database_gets_nullable_seed_columns_and_remains_immutable(seeded):
    _, session, _, _, version, _ = seeded
    engine = session.get_bind()
    session.commit()
    with engine.begin() as connection:
        for name in ("reference_path", "reference_sha256", "runtime_snapshot"):
            connection.exec_driver_sql(
                f"ALTER TABLE singing_voice_model_versions DROP COLUMN {name}"
            )
    init_voice_models(engine)
    init_voice_models(engine)
    assert {"reference_path", "reference_sha256", "runtime_snapshot"} <= {
        item["name"] for item in inspect(engine).get_columns("singing_voice_model_versions")
    }
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("UPDATE singing_voice_model_versions SET provenance='changed' WHERE id=:id"),
            {"id": version.id},
        )


def test_worker_attenuates_float_peaks_and_pads_without_stretching():
    np = pytest.importorskip("numpy")
    source = np.sin(np.arange(44100) * 0.047).reshape(-1, 1)
    raw = np.cos(np.arange(44000) * 0.054) * 1.2
    normalized, info = normalize_audio(raw, source, 44100, 44100, 0.01)
    assert normalized.size == 44100 and info["tail_padding_samples"] == 100
    assert np.max(np.abs(normalized)) <= 10 ** (-1 / 20) + 1e-6
    assert not info["time_stretch_applied"] and info["raw_above_full_scale_fraction"] > 0
    assert np.all(normalized[-100:] == 0)


@pytest.mark.parametrize("kind", ["nan", "silent", "long", "short", "passthrough"])
def test_worker_rejects_invalid_or_original_singer_audio(kind):
    np = pytest.importorskip("numpy")
    source = np.sin(np.arange(44100) * 0.047).reshape(-1, 1)
    raw = np.cos(np.arange(44100) * 0.054)
    if kind == "nan":
        raw[0] = np.nan
    elif kind == "silent":
        raw *= 0
    elif kind == "long":
        raw = np.pad(raw, (0, 1))
    elif kind == "short":
        raw = raw[:-442]
    else:
        raw = source[:, 0] * 0.5
    with pytest.raises(ValueError):
        normalize_audio(raw, source, 44100, 44100, 0.01)
