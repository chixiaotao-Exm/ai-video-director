"""CPU-only process evidence and transactional ownership checks; no model calls."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avd.backends.comfyui import ComfyUIClient, ComfyUIError
from avd.config import LLMRoute, Settings
from avd.jobs import native
from avd.jobs.execution import execution_data, record_request
from avd.jobs.models import Job, JobStatus
from avd.jobs.native import native_execution_observer, native_model_dispatch, observe_execution
from avd.performance import seed_vc, voice
from avd.runtime import Runtime


def emit() -> None:
    observe_execution(
        role="singing_voice",
        provider="local",
        model="SeedVC",
        phase="probe",
        details={"engine": "seed-vc", "voice_model_version_id": "fixture-version"},
    )


def run_script(script: str, *, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return native.run_model_process(
        [sys.executable, "-c", script],
        role="singing_voice",
        model="CPU fixture",
        phase="probe",
        details={"runtime_revision": "offline-fixture"},
        timeout=timeout,
    )


@pytest.mark.parametrize("outcome", ["success", "nonzero", "timeout", "missing-executable"])
def test_native_launch_evidence_survives_failures_only_after_popen_succeeds(outcome):
    seen = []
    with native_execution_observer(seen.append):
        if outcome == "success":
            result = run_script("print('CPU fixture output')")
            assert result.stdout.strip() == "CPU fixture output" and result.returncode == 0
        elif outcome == "nonzero":
            with pytest.raises(subprocess.CalledProcessError) as error:
                run_script("import sys; print('fixture failure', file=sys.stderr); sys.exit(7)")
            assert error.value.returncode == 7 and "fixture failure" in error.value.stderr
        elif outcome == "timeout":
            with pytest.raises(subprocess.TimeoutExpired):
                run_script("import time; time.sleep(30)", timeout=0.05)
        else:
            with pytest.raises(FileNotFoundError):
                native.run_model_process(
                    ["avd-intentionally-missing-executable-56d125"],
                    role="singing_voice",
                    model="CPU fixture",
                    phase="probe",
                    details={},
                    timeout=10,
                )
    assert len(seen) == (0 if outcome == "missing-executable" else 1)
    if seen:
        assert seen[0]["source"] == "actual"
        assert seen[0]["details"]["process_id"] > 0
        assert seen[0]["details"]["evidence"] == "process_started"
    emit()
    assert len(seen) == (0 if outcome == "missing-executable" else 1)


def test_rejected_execution_record_terminates_the_started_process(monkeypatch):
    processes = []
    real_popen = subprocess.Popen

    def tracked(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def rejected(event):
        assert event["details"]["process_id"] == processes[0].pid
        raise ValueError("claim lost")

    monkeypatch.setattr(native.subprocess, "Popen", tracked)
    with native_execution_observer(rejected), pytest.raises(ValueError, match="claim lost"):
        run_script("import time; time.sleep(30)")
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize("engine", ["rvc", "seed-vc"])
def test_injected_singing_runner_cannot_claim_actual_execution(tmp_path, engine):
    common = {
        "project_root": tmp_path,
        "python_executable": Path(sys.executable),
        "revision": "a" * 40,
    }
    seen = []

    def fake(*args, **kwargs):
        return SimpleNamespace(stdout="explicit fake output", stderr="")

    with native_execution_observer(seen.append):
        if engine == "rvc":
            result = voice._run(["fake", "--list-speakers"], voice.RVCInstallation(**common), fake)
        else:
            installation = voice.SeedVCInstallation(
                **common, model_manifest_path=tmp_path / "unused-manifest.json"
            )
            result = seed_vc._run(["fake", "--probe"], installation, fake)
    assert result == "explicit fake output" and seen == []


@pytest.mark.parametrize("engine", ["rvc", "seed-vc"])
def test_default_singing_runner_records_selected_engine_and_probe_phase(tmp_path, engine):
    common = {
        "project_root": tmp_path,
        "python_executable": Path(sys.executable),
        "revision": "a" * 40,
    }
    arguments = [sys.executable, "-c", "print('CPU adapter contract fixture')"]
    seen = []
    with native_execution_observer(seen.append):
        if engine == "rvc":
            result = voice._run(
                arguments,
                voice.RVCInstallation(**common),
                None,
                details={"voice_model_version_id": "selected-fixture-version"},
            )
        else:
            result = seed_vc._run(
                [*arguments, "--probe"],
                voice.SeedVCInstallation(
                    **common, model_manifest_path=tmp_path / "unused-manifest.json"
                ),
                None,
                details={"voice_model_version_id": "selected-fixture-version"},
            )
    assert result.strip() == "CPU adapter contract fixture"
    assert len(seen) == 1 and seen[0]["phase"] == "probe"
    assert seen[0]["model"] == ("RVC" if engine == "rvc" else "SeedVC")
    assert seen[0]["details"]["engine"] == engine
    assert seen[0]["details"]["voice_model_version_id"] == "selected-fixture-version"
    assert seen[0]["details"]["runtime_revision"] == "a" * 40


def test_native_comfy_client_records_only_prompt_dispatch_and_keeps_failed_attempt():
    seen, requests = [], []
    identity = {
        "role": "motion_video",
        "provider": "comfyui",
        "model": "declared-fixture-model",
        "phase": "submit",
        "details": {"remote_weights_verified": False},
    }

    def transport(request):
        requests.append(request.url.path)
        assert len(seen) == 1 and request.url.path == "/prompt"
        return httpx.Response(503, json={"error": "explicit offline transport error"})

    with (
        ComfyUIClient("http://127.0.0.1:8188", transport=httpx.MockTransport(transport)) as client,
        native_execution_observer(seen.append),
        native_model_dispatch(identity),
    ):
        with pytest.raises(TypeError):
            client.submit([])
        assert seen == []
        with pytest.raises(ComfyUIError):
            client.submit({"1": {"class_type": "OfflineFixture", "inputs": {}}})
    assert requests == ["/prompt"] and seen[0]["source"] == "actual"
    assert seen[0]["details"]["remote_weights_verified"] is False

    class ExplicitFakeClient(ComfyUIClient):
        def submit(self, workflow):
            return "explicit-fake-prompt"

    with (
        ExplicitFakeClient("http://127.0.0.1:8188") as fake,
        native_execution_observer(seen.append),
        native_model_dispatch(identity),
    ):
        assert fake.submit({}) == "explicit-fake-prompt"
    assert len(seen) == 1


def test_execution_phases_resume_and_details_are_distinct_immutable_evidence():
    job = Job(job_type="singing_voice", payload={}, attempts=1, status=JobStatus.RUNNING)
    route = LLMRoute(provider="local", model="SeedVC")
    details = {"weights": [{"sha256": "a" * 64}]}
    record_request(job, "singing_voice", route, phase="probe", details=details)
    details["weights"][0]["sha256"] = "b" * 64
    record_request(job, "singing_voice", route, phase="inference", details=details)
    record_request(job, "singing_voice", route, phase="resume", details=details, source="resumed")
    steps = execution_data(job)["steps"]
    assert [step["phase"] for step in steps] == ["probe", "inference", "resume"]
    assert steps[0]["details"]["weights"][0]["sha256"] == "a" * 64
    assert steps[-1]["calls"] == 0 and "requested_at" not in steps[-1]
    job.status, job.attempts = JobStatus.QUEUED, 2
    assert execution_data(job)["source"] != "actual"
    assert len(execution_data(job)["history"]) == 3


@pytest.mark.parametrize("stage", ["performance_prepare", "performance_mix", "performance_final"])
def test_deterministic_tools_have_only_snapshot_identity(stage):
    job = Job(job_type=stage, payload={}, attempts=1, status=JobStatus.SUCCEEDED)
    view = execution_data(job)
    assert view["source"] == "snapshot" and view["model"] == "FFmpeg"
    assert view["history"] == [] and "calls" not in view


@pytest.fixture
def claimed_probe(tmp_path):
    runtime = Runtime(Settings(_env_file=None, data_dir=tmp_path))
    job_id = runtime.queue.submit("gpu", "singing_model_probe", {"version_id": "fixture-version"})
    job = runtime.queue.claim_next("gpu")
    assert job is not None and job.id == job_id
    try:
        yield runtime, job
    finally:
        runtime.close()


def test_global_probe_commits_actual_identity_even_when_handler_later_fails(
    claimed_probe, monkeypatch
):
    runtime, job = claimed_probe

    def probe(session, *args, **kwargs):
        assert session.get(Job, job.id).claim_token == job.claim_token
        emit()
        raise RuntimeError("explicit post-launch probe failure")

    monkeypatch.setattr(voice, "voice_model_readiness", probe)
    handler = runtime.build_registry().get("singing_model_probe")
    with pytest.raises(RuntimeError, match="post-launch probe failure"):
        handler(job)
    saved = runtime.queue.get(job.id)
    view = execution_data(saved)
    assert view["source"] == "actual" and view["phase"] == "probe"
    assert view["details"]["voice_model_version_id"] == "fixture-version"
    assert job.project_id is None and job.node_id is None


@pytest.mark.parametrize("changed", ["status", "token", "attempt", "empty-claim"])
def test_late_native_event_cannot_modify_another_or_terminal_attempt(
    claimed_probe, monkeypatch, changed
):
    runtime, job = claimed_probe
    if changed == "empty-claim":
        job.claim_token = None
    else:
        with runtime.session_factory() as session:
            saved = session.get(Job, job.id)
            if changed == "status":
                saved.status = JobStatus.FAILED
            elif changed == "token":
                saved.claim_token = "replacement-claim-token"
            else:
                saved.attempts += 1
            session.commit()

    def probe(*args, **kwargs):
        emit()
        pytest.fail("Rejected ownership must stop the handler")

    monkeypatch.setattr(voice, "voice_model_readiness", probe)
    handler = runtime.build_registry().get("singing_model_probe")
    with pytest.raises(ValueError, match="no longer belongs"):
        handler(job)
    saved = runtime.queue.get(job.id)
    assert saved.payload.get("_execution_steps", []) == []


def test_fake_probe_does_not_manufacture_native_model_evidence(claimed_probe, monkeypatch):
    runtime, job = claimed_probe
    monkeypatch.setattr(
        voice,
        "voice_model_readiness",
        lambda *args, **kwargs: SimpleNamespace(model_dump=lambda **kwargs: {"fake": True}),
    )
    handler = runtime.build_registry().get("singing_model_probe")
    assert handler(job) == {"readiness": {"fake": True}}
    assert runtime.queue.get(job.id).payload.get("_execution_steps", []) == []
