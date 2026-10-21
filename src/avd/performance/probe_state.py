"""Restore compatible persisted model probes without starting inference on a GET."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.jobs.models import Job, JobStatus

from .voice import (
    RVCInstallation,
    RVCParameters,
    SeedVCInstallation,
    SingingVoiceModelVersion,
    VoiceModelReadiness,
    rvc_installation_snapshot,
    voice_model_readiness,
)


def probe_payload_matches(payload: dict[str, Any], captured: dict[str, Any]) -> bool:
    """Execution evidence appended to a job is not part of its probe configuration."""
    return all(key in payload and payload[key] == value for key, value in captured.items())


def restore_voice_probe(
    session: Session,
    current: VoiceModelReadiness,
    *,
    installation: RVCInstallation | None = None,
    seed_installation: SeedVCInstallation | None = None,
    parameters: dict[str, Any] | None = None,
) -> VoiceModelReadiness:
    """Reuse only the newest matching probe after current file/consent checks pass."""
    if current.ready_for_inference or not (
        current.imported_files_valid and current.runtime_files_available
    ):
        return current
    version = session.get(SingingVoiceModelVersion, current.version_id)
    if version is None:
        return current
    is_seed = version.engine == "seed-vc"
    configured = seed_installation if is_seed else installation
    payload = {
        "version_id": version.id,
        "seed_vc_installation" if is_seed else "rvc_installation": (
            configured.model_dump(mode="json") if configured else None
        ),
    }
    latest = next(
        (
            job
            for job in session.scalars(
                select(Job)
                .where(Job.job_type == "singing_model_probe")
                .order_by(Job.created_at.desc(), Job.id.desc())
            )
            if probe_payload_matches(job.payload, payload)
        ),
        None,
    )
    if latest is None:
        return current
    current = current.model_copy(
        update={"probe_job_id": latest.id, "probe_status": latest.status.value}
    )
    if latest.status != JobStatus.SUCCEEDED:
        return current.model_copy(
            update={"reasons": [f"The latest singing model probe is {latest.status.value}"]}
        )
    try:
        cached = VoiceModelReadiness.model_validate((latest.result or {}).get("readiness"))
    except (ValidationError, TypeError):
        return current
    if not (
        cached.version_id == current.version_id
        and cached.engine == current.engine
        and cached.model_fingerprint == current.model_fingerprint
    ):
        return current
    if not (
        cached.ready_for_inference
        and cached.runtime_available
        and cached.model_compatible
        and cached.imported_files_valid
        and cached.runtime_files_available
    ):
        return current.model_copy(
            update={
                "reasons": ["The latest model probe did not confirm compatibility", *cached.reasons]
            }
        )
    cli_hash, source_hash = current.cli_sha256, current.runtime_source_sha256
    if not is_seed:
        snapshot = rvc_installation_snapshot(installation)
        if snapshot["not_ready"]:
            return current
        cli_hash, source_hash = snapshot["cli_sha256"], snapshot["runtime_source_sha256"]
        try:
            selected = RVCParameters.model_validate(
                version.parameters if parameters is None else parameters
            )
        except ValidationError:
            return current
        if selected.speaker_id not in cached.speaker_ids:
            return current.model_copy(
                update={"reasons": ["Configured speaker_id is not present in the RVC checkpoint"]}
            )
    if not (
        cli_hash
        and source_hash
        and cached.cli_sha256 == cli_hash
        and cached.runtime_source_sha256 == source_hash
    ):
        return current
    return cached.model_copy(
        update={
            "quality_verified": False,
            "probe_job_id": latest.id,
            "probe_status": latest.status.value,
        }
    )


def persisted_voice_readiness(
    session: Session,
    data_dir: Path,
    version_id: str,
    *,
    installation: RVCInstallation | None = None,
    seed_installation: SeedVCInstallation | None = None,
    parameters: dict[str, Any] | None = None,
) -> VoiceModelReadiness:
    current = voice_model_readiness(
        session,
        data_dir,
        version_id,
        installation=installation,
        seed_installation=seed_installation,
    )
    return restore_voice_probe(
        session,
        current,
        installation=installation,
        seed_installation=seed_installation,
        parameters=parameters,
    )
