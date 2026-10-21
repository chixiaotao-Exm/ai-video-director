"""Singing model lifecycle and official CLI contract, using no model/network calls."""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from avd.jobs.models import Job, JobStatus
from avd.performance import voice as singing
from avd.performance.probe_state import persisted_voice_readiness
from avd.performance.voice import (
    RVCInstallation,
    RVCParameters,
    RVCVoiceConverter,
    SingingVoiceError,
    SingingVoiceModelVersion,
    VoiceModelImport,
    import_voice_model,
    init_voice_models,
    rvc_installation_snapshot,
    validate_rvc_snapshot,
    voice_model_readiness,
)
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Voice


def wav(
    path: Path,
    *,
    sample_rate: int = 40000,
    duration_s: float = 1.0,
    sample_count: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        count = sample_count if sample_count is not None else round(sample_rate * duration_s)
        handle.writeframes(b"\x00\x00" * count)


@pytest.fixture
def model(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'state.db').as_posix()}")
    init_db(engine)
    init_voice_models(engine)
    init_voice_models(engine)
    factory = create_session_factory(engine)
    for name, content in (
        ("target.pth", b"OFFLINE FAKE CHECKPOINT"),
        ("features.index", b"FAKE INDEX"),
        ("training.json", b'{"version":"v2"}'),
    ):
        (tmp_path / name).write_bytes(content)
    cli = tmp_path / "rvc-runtime" / "infer" / "cli.py"
    cli.parent.mkdir(parents=True)
    cli.write_text("# fake CLI, subprocess is injected by tests", encoding="utf-8")
    installation = RVCInstallation(
        project_root=cli.parents[1],
        python_executable=Path(sys.executable),
        revision="test-revision",
    )
    with factory() as session:
        voice = Voice(
            name="consented target", sample_audio_paths=["sample.wav"], consent_confirmed=True
        )
        session.add(voice)
        session.flush()
        request = VoiceModelImport(
            voice_id=voice.id,
            model_path="target.pth",
            index_path="features.index",
            config_path="training.json",
            sample_rate=40000,
            provenance="offline fixture",
            parameters=RVCParameters(index_rate=0.5),
        )
        version = import_voice_model(session, tmp_path, request)
        session.commit()
        yield tmp_path, session, request, version, installation
    engine.dispose()


def runner(*, sample_rate=40000, duration_s=1.0, sample_count=None, speaker_ids=None):
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        assert kwargs["shell"] is False
        if "-c" in arguments:
            return SimpleNamespace(
                stdout='AVD_RVC_RUNTIME={"device":"cpu","dtype":"torch.float32"}'
            )
        if "--list-speakers" in arguments:
            return SimpleNamespace(
                stdout="\n".join(
                    f"{speaker}\tSpeaker {speaker}" for speaker in (speaker_ids or [0, 1])
                )
            )
        wav(
            Path(arguments[arguments.index("--output") + 1]),
            sample_rate=sample_rate,
            duration_s=duration_s,
            sample_count=sample_count,
        )
        return SimpleNamespace(stdout="Conversion completed")

    return run, calls


def test_import_snapshots_files_and_is_idempotent(model):
    root, session, request, version, _ = model
    assert not Path(version.model_path).is_absolute()
    assert (root / version.model_path).read_bytes() == b"OFFLINE FAKE CHECKPOINT"
    assert import_voice_model(session, root, request).id == version.id
    assert len(session.scalars(select(SingingVoiceModelVersion)).all()) == 1
    (root / "target.pth").write_bytes(b"NEW OFFLINE MODEL")
    new = import_voice_model(session, root, request)
    assert new.number == 2 and new.fingerprint != version.fingerprint
    assert (root / version.model_path).read_bytes() == b"OFFLINE FAKE CHECKPOINT"


def test_model_import_does_not_certify_runtime_or_quality(model):
    root, session, _, version, installation = model
    check = voice_model_readiness(session, root, version.id)
    assert check.imported_files_valid and not check.ready_for_inference
    check = voice_model_readiness(session, root, version.id, installation=installation)
    assert not check.ready_for_inference and not check.quality_verified
    run, _ = runner()
    ready = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    assert ready.ready_for_inference and ready.model_compatible
    assert ready.speaker_ids == [0, 1] and not ready.quality_verified


def save_probe(session, version, installation, readiness, *, status=JobStatus.SUCCEEDED):
    job = Job(
        lane="gpu",
        job_type="singing_model_probe",
        status=status,
        payload={
            "version_id": version.id,
            "rvc_installation": installation.model_dump(mode="json"),
            "_execution_steps": [{"model": "RVC", "phase": "probe", "source": "actual"}],
        },
        result={"readiness": readiness.model_dump(mode="json")},
    )
    session.add(job)
    session.commit()
    return job


def test_persisted_probe_survives_a_new_session_without_starting_a_process(model, monkeypatch):
    root, session, _, version, installation = model
    run, _ = runner()
    ready = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    job = save_probe(session, version, installation, ready)

    def forbidden(*args, **kwargs):
        raise AssertionError("Restoring a persisted probe must never start a process")

    monkeypatch.setattr(singing, "run_model_process", forbidden)
    with type(session)(bind=session.get_bind()) as reopened:
        for _ in range(2):  # Reopening or switching back to a model uses the same durable check.
            restored = persisted_voice_readiness(
                reopened, root, version.id, installation=installation
            )
            assert restored.ready_for_inference and restored.runtime_available
            assert restored.speaker_ids == [0, 1] and not restored.quality_verified
            assert restored.probe_job_id == job.id and restored.probe_status == "succeeded"


@pytest.mark.parametrize("status", list(JobStatus))
def test_newest_matching_probe_cannot_borrow_an_older_success(model, status):
    root, session, _, version, installation = model
    run, _ = runner()
    ready = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    save_probe(session, version, installation, ready)
    unsuccessful = ready.model_copy(update={"ready_for_inference": False})
    save_probe(session, version, installation, unsuccessful, status=status)
    restored = persisted_voice_readiness(session, root, version.id, installation=installation)
    assert not restored.ready_for_inference and not restored.quality_verified


@pytest.mark.parametrize(
    "change", ["source", "model", "consent", "configuration", "fingerprint", "malformed"]
)
def test_persisted_probe_rechecks_files_runtime_consent_and_result(model, change):
    root, session, _, version, installation = model
    run, _ = runner()
    ready = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    job = save_probe(session, version, installation, ready)
    if change == "source":
        (installation.project_root / "infer" / "changed.py").write_text("# changed")
    elif change == "model":
        (root / version.model_path).write_bytes(b"changed checkpoint")
    elif change == "consent":
        session.get(Voice, version.voice_id).consent_confirmed = False
    elif change == "configuration":
        installation = installation.model_copy(update={"timeout_s": 1})
    elif change == "fingerprint":
        job.result = {
            "readiness": ready.model_copy(update={"model_fingerprint": "other"}).model_dump(
                mode="json"
            )
        }
    else:
        job.result = {"readiness": {"ready_for_inference": True}}
    session.commit()
    restored = persisted_voice_readiness(session, root, version.id, installation=installation)
    assert not restored.ready_for_inference


def test_persisted_rvc_probe_rejects_an_unavailable_selected_speaker(model):
    root, session, _, version, installation = model
    run, _ = runner()
    ready = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    save_probe(session, version, installation, ready)
    restored = persisted_voice_readiness(
        session, root, version.id, installation=installation, parameters={"speaker_id": 99}
    )
    assert not restored.ready_for_inference and "speaker_id" in restored.reasons[0]


def test_completed_but_incompatible_probe_retains_the_actual_diagnostic(model):
    root, session, _, version, installation = model
    run, _ = runner(speaker_ids=[2])
    incompatible = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    assert not incompatible.ready_for_inference
    job = save_probe(session, version, installation, incompatible)
    restored = persisted_voice_readiness(session, root, version.id, installation=installation)
    assert not restored.ready_for_inference
    assert any("speaker_id" in reason for reason in restored.reasons)
    assert restored.probe_job_id == job.id and restored.probe_status == "succeeded"


def test_runtime_files_can_be_checked_without_loading_a_model(model):
    root, session, _, version, installation = model
    run, calls = runner()
    check = voice_model_readiness(session, root, version.id, installation=installation, runner=run)
    assert check.imported_files_valid and check.runtime_files_available
    assert not check.runtime_available and not check.model_compatible
    assert not check.ready_for_inference and not check.quality_verified
    assert not calls


def test_runtime_snapshot_and_validation_never_probe_a_model(model, monkeypatch):
    _, _, _, _, installation = model

    def no_subprocess(*args, **kwargs):
        raise AssertionError("A filesystem snapshot must not start a subprocess")

    monkeypatch.setattr(singing.subprocess, "run", no_subprocess)
    snapshot = rvc_installation_snapshot(installation)
    assert snapshot["installation"] == installation.model_dump(mode="json")
    assert snapshot["not_ready"] is False and snapshot["reasons"] == []
    assert len(snapshot["cli_sha256"]) == len(snapshot["runtime_source_sha256"]) == 64
    validate_rvc_snapshot(installation, json.loads(json.dumps(snapshot)))


def test_missing_installation_is_explicitly_not_ready():
    snapshot = rvc_installation_snapshot(None)
    assert snapshot["installation"] is None and snapshot["not_ready"] is True
    assert snapshot["cli_sha256"] is None and snapshot["runtime_source_sha256"] is None
    assert snapshot["reasons"]
    with pytest.raises(SingingVoiceError, match="not ready"):
        validate_rvc_snapshot(None, snapshot)


@pytest.mark.parametrize("missing", ["python", "cli"])
def test_unavailable_runtime_snapshot_cannot_be_reused_after_installation(model, missing):
    root, _, _, _, installation = model
    if missing == "python":
        installation.python_executable = root / "not-yet-installed-python.exe"
    else:
        installation.cli().unlink()
    snapshot = rvc_installation_snapshot(installation)
    assert snapshot["not_ready"] is True and snapshot["reasons"]
    if missing == "python":
        installation.python_executable.write_bytes(b"installed later")
    else:
        installation.cli().write_text("# installed later", encoding="utf-8")
    with pytest.raises(SingingVoiceError, match="not ready"):
        validate_rvc_snapshot(installation, snapshot)


@pytest.mark.parametrize(
    "field,value",
    [("revision", "new-runtime"), ("timeout_s", 900), ("duration_tolerance_s", 0.001)],
)
def test_runtime_snapshot_rejects_changed_configuration(model, field, value):
    _, _, _, _, installation = model
    snapshot = rvc_installation_snapshot(installation)
    changed = installation.model_copy(update={field: value})
    with pytest.raises(SingingVoiceError, match="configuration changed"):
        validate_rvc_snapshot(changed, snapshot)


@pytest.mark.parametrize("change", ["cli", "new_source", "removed_source", "missing_python"])
def test_runtime_snapshot_rejects_mutated_or_missing_files(model, change):
    root, _, _, _, installation = model
    source = installation.project_root / "configs" / "config.py"
    source.parent.mkdir()
    source.write_text("# frozen config", encoding="utf-8")
    python = root / "runtime-python.exe"
    python.write_bytes(b"fake executable; snapshots do not run it")
    installation.python_executable = python
    snapshot = rvc_installation_snapshot(installation)
    if change == "cli":
        installation.cli().write_text("# updated CLI", encoding="utf-8")
    elif change == "new_source":
        source.with_name("new.py").write_text("# new module", encoding="utf-8")
    elif change == "removed_source":
        source.unlink()
    else:
        python.unlink()
    with pytest.raises(SingingVoiceError, match="changed|unavailable"):
        validate_rvc_snapshot(installation, snapshot)


def test_runtime_snapshot_honors_configured_cli_hash(model):
    _, _, _, _, installation = model
    installation.cli_sha256 = "0" * 64
    snapshot = rvc_installation_snapshot(installation)
    assert snapshot["not_ready"] is True
    assert "CLI changed" in snapshot["reasons"][0]


def test_runtime_snapshot_requires_hashes(model):
    _, _, _, _, installation = model
    snapshot = rvc_installation_snapshot(installation)
    snapshot.pop("runtime_source_sha256")
    with pytest.raises(SingingVoiceError, match="source files changed"):
        validate_rvc_snapshot(installation, snapshot)


def test_voice_samples_are_never_treated_as_a_model(model):
    root, session, _, _, _ = model
    result = voice_model_readiness(session, root, "sample-only-voice")
    assert not result.ready_for_inference and not result.imported_files_valid
    assert "ordinary voice samples" in result.reasons[0]


def test_revoke_consent_prevents_import_and_inference(model):
    root, session, request, version, installation = model
    target = session.get(Voice, request.voice_id)
    target.consent_confirmed = False
    session.flush()
    with pytest.raises(PermissionError):
        import_voice_model(session, root, request)
    ready = voice_model_readiness(session, root, version.id, installation=installation, probe=True)
    assert not ready.imported_files_valid and not ready.ready_for_inference


@pytest.mark.parametrize("path", ["../target.pth", "D:\\outside\\model.pth", "/outside/model.pth"])
def test_import_rejects_nonrelative_model_paths(path):
    with pytest.raises(ValidationError):
        VoiceModelImport(voice_id="v", model_path=path, sample_rate=40000, provenance="test")


def test_import_rejects_index_rate_without_index():
    with pytest.raises(ValidationError, match="index_rate"):
        VoiceModelImport(
            voice_id="v",
            model_path="model.pth",
            sample_rate=40000,
            provenance="test",
            parameters=RVCParameters(index_rate=0.7),
        )


def test_snapshot_mutation_or_missing_runtime_prevents_readiness(model):
    root, session, _, version, installation = model
    run, _ = runner()
    (root / version.model_path).write_bytes(b"changed")
    result = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    assert not result.ready_for_inference and "changed" in result.reasons[0]


def test_model_versions_are_immutable_through_orm(model):
    _, session, _, version, _ = model
    version.sample_rate = 48000
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()
    version = session.get(SingingVoiceModelVersion, version.id)
    session.delete(version)
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE singing_voice_model_versions SET sample_rate = 48000",
        "DELETE FROM singing_voice_model_versions",
    ],
)
def test_model_versions_are_immutable_through_sql(model, statement):
    _, session, _, _, _ = model
    with pytest.raises(IntegrityError, match="immutable"):
        session.connection().exec_driver_sql(statement)
    session.rollback()


def test_cli_conversion_uses_official_flags_and_records_provenance(model):
    root, session, _, version, installation = model
    wav(root / "input's singing.wav", sample_rate=48000)
    run, calls = runner()
    converter = RVCVoiceConverter(root, installation, runner=run)
    result = converter.convert(session, version.id, "input's singing.wav", "outputs/converted.wav")
    assert len(calls) == 3
    arguments, options = calls[-1]
    assert arguments[:2] == [str(Path(sys.executable).resolve()), str(installation.cli())]
    assert arguments[arguments.index("--input") + 1] == str(root / "input's singing.wav")
    assert arguments[arguments.index("--f0-method") + 1] == "rmvpe"
    assert arguments[arguments.index("--index-rate") + 1] == "0.5"
    assert arguments[arguments.index("--format") + 1] == "wav"
    assert "--device" not in arguments and "--filter-radius" not in arguments
    expected_flags = {
        "--model",
        "--input",
        "--output",
        "--format",
        "--speaker-id",
        "--pitch",
        "--f0-method",
        "--index-rate",
        "--resample-sr",
        "--rms-mix-rate",
        "--protect",
        "--index",
    }
    assert {arg for arg in arguments if arg.startswith("--")} == expected_flags
    assert options["cwd"] == installation.project_root and options["timeout"] == 600
    assert result["output"]["sample_rate"] == 40000 and result["duration_delta_s"] == 0
    assert not result["quality_verified"] and len(result["configuration_hash"]) == 64
    stored = json.loads((root / result["manifest_path"]).read_text(encoding="utf-8"))
    assert stored["input"]["path"] == "input's singing.wav"
    assert stored["model_fingerprint"] == version.fingerprint
    assert stored["runtime_device"] == "cpu" and stored["runtime_dtype"] == "torch.float32"
    assert len(stored["runtime_source_sha256"]) == 64


def test_conversion_never_overwrites_outputs(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    wav(root / "existing.wav")
    before = (root / "existing.wav").read_bytes()
    run, calls = runner()
    with pytest.raises(FileExistsError):
        RVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "input.wav", "existing.wav"
        )
    assert (root / "existing.wav").read_bytes() == before
    assert len(calls) == 2  # readiness only


def test_same_destination_conversion_is_exclusive(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    run, calls = runner()
    nested_converter = RVCVoiceConverter(root, installation, runner=run)

    def reentrant(arguments, **kwargs):
        if "--output" in arguments:
            assert not (root / "outputs/converted.wav").exists()
            with pytest.raises(FileExistsError, match="already in progress"):
                nested_converter.convert(session, version.id, "input.wav", "outputs/converted.wav")
        return run(arguments, **kwargs)

    result = RVCVoiceConverter(root, installation, runner=reentrant).convert(
        session, version.id, "input.wav", "outputs/converted.wav"
    )
    assert len([args for args, _ in calls if "--output" in args]) == 1
    assert (root / result["output"]["path"]).is_file()
    assert (root / result["manifest_path"]).is_file()
    assert not (root / "outputs/converted.conversion.lock").exists()


def test_conversion_preserves_another_owner_lock(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    lock = root / "output.conversion.lock"
    lock.write_text("another conversion", encoding="utf-8")
    run, calls = runner()
    with pytest.raises(FileExistsError, match="already in progress"):
        RVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "input.wav", "output.wav"
        )
    assert len(calls) == 2
    assert lock.read_text(encoding="utf-8") == "another conversion"
    assert not (root / "output.wav").exists()


@pytest.mark.parametrize(
    "rate,duration,error",
    [(48000, 1, "sample rate"), (40000, 1.2, "duration"), (40000, 1.02, "duration")],
)
def test_conversion_rejects_wrong_output_without_stretching(model, rate, duration, error):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    run, _ = runner(sample_rate=rate, duration_s=duration)
    with pytest.raises(SingingVoiceError, match=error):
        RVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "input.wav", "output.wav"
        )
    assert not (root / "output.wav").exists()
    assert not (root / "output.conversion.json").exists()
    assert not (root / "output.conversion.lock").exists()


@pytest.mark.parametrize("input_rate,output_rate", [(40000, 40000), (48000, 40000), (32000, 48000)])
@pytest.mark.parametrize("sample_delta,accepted", [(-2, False), (-1, True), (1, True), (2, False)])
def test_conversion_accepts_at_most_one_sample_of_timing_error(
    model, input_rate, output_rate, sample_delta, accepted
):
    root, session, _, version, installation = model
    wav(root / "input.wav", sample_rate=input_rate)
    run, _ = runner(sample_rate=output_rate, sample_count=output_rate + sample_delta)
    parameters = RVCParameters(resample_sr=output_rate)
    converter = RVCVoiceConverter(root, installation, runner=run)
    if accepted:
        result = converter.convert(
            session, version.id, "input.wav", "output.wav", parameters=parameters
        )
        assert result["output"]["sample_count"] == output_rate + sample_delta
        assert result["duration_tolerance_s"] == max(1 / input_rate, 1 / output_rate)
    else:
        with pytest.raises(SingingVoiceError, match="duration"):
            converter.convert(session, version.id, "input.wav", "output.wav", parameters=parameters)
        assert not (root / "output.wav").exists()


def test_conversion_honors_stricter_configured_timing_tolerance(model):
    root, session, _, version, installation = model
    installation.duration_tolerance_s = 0.00001
    wav(root / "input.wav")
    run, _ = runner(sample_count=40001)
    with pytest.raises(SingingVoiceError, match="duration"):
        RVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "input.wav", "output.wav"
        )
    assert not (root / "output.wav").exists()


def test_conversion_rejects_runtime_mutation_during_inference(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    run, _ = runner()

    def mutate(arguments, **kwargs):
        result = run(arguments, **kwargs)
        if "--output" in arguments:
            installation.cli().write_text("# runtime changed while running", encoding="utf-8")
        return result

    with pytest.raises(SingingVoiceError, match="source files changed"):
        RVCVoiceConverter(root, installation, runner=mutate).convert(
            session, version.id, "input.wav", "output.wav"
        )
    assert not (root / "output.wav").exists()
    assert not (root / "output.conversion.json").exists()
    assert not (root / "output.conversion.lock").exists()


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired("rvc", 1),
        subprocess.CalledProcessError(2, "rvc", stderr="checkpoint incompatible"),
    ],
)
def test_cli_failures_return_actionable_errors(model, error):
    root, session, _, version, installation = model

    def fail(*args, **kwargs):
        raise error

    result = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=fail
    )
    assert not result.ready_for_inference and result.reasons


def test_override_cannot_use_unknown_speaker(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    run, calls = runner()
    with pytest.raises(SingingVoiceError, match="speaker_id"):
        RVCVoiceConverter(root, installation, runner=run).convert(
            session, version.id, "input.wav", "out.wav", parameters=RVCParameters(speaker_id=5)
        )
    assert len(calls) == 2


def test_runtime_hash_change_invalidates_readiness(model):
    root, session, _, version, installation = model
    installation.cli_sha256 = "0" * 64
    run, calls = runner()
    result = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    assert not result.ready_for_inference and "CLI changed" in result.reasons[0]
    assert not calls


def test_failed_snapshot_copy_cleans_only_new_folder(model, monkeypatch):
    root, session, request, _, _ = model
    (root / "target.pth").write_bytes(b"another version")

    def fail(source, destination, length):
        destination.write(b"partial")
        raise OSError("copy interrupted")

    monkeypatch.setattr(singing.shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="copy interrupted"):
        import_voice_model(session, root, request)
    assert len(list((root / "assets" / "voice-models" / request.voice_id).iterdir())) == 1


@pytest.mark.parametrize(
    "text,expected", [("0-0\n", [0]), ("0-3\n", [0, 1, 2, 3]), ("0\tAlice\n3\tBob\n", [0, 3])]
)
def test_official_cli_speaker_text_contract(text, expected):
    assert singing._speaker_ids(text) == expected


def test_runtime_cannot_silently_fall_back_to_cpu(model):
    root, session, _, version, installation = model
    installation.device = "cuda"
    run, calls = runner()
    result = voice_model_readiness(
        session, root, version.id, installation=installation, probe=True, runner=run
    )
    assert not result.ready_for_inference and "different device" in result.reasons[0]
    assert len(calls) == 1


def test_conversion_accepts_declared_nondefault_speaker_override(model):
    root, session, _, version, installation = model
    wav(root / "input.wav")
    run, calls = runner(speaker_ids=[1])
    result = RVCVoiceConverter(root, installation, runner=run).convert(
        session, version.id, "input.wav", "output.wav", parameters=RVCParameters(speaker_id=1)
    )
    assert result["parameters"]["speaker_id"] == 1
    assert len(calls) == 3


@pytest.mark.parametrize("method", ["harvest", "crepe"])
def test_methods_missing_from_supported_cli_are_rejected(method):
    with pytest.raises(ValidationError):
        RVCParameters(f0_method=method)
