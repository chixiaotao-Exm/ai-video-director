"""Input configuration and deterministic preparation for performance projects."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from fastapi import FastAPI, HTTPException
from sqlalchemy import select, text

from avd.jobs.models import Job, JobStatus
from avd.performance.motion import motion_workflow_readiness
from avd.performance.planning import SegmentPlanRequest
from avd.performance.probe_state import persisted_voice_readiness, probe_payload_matches
from avd.performance.service import (
    PerformanceConfigPatch,
    PerformanceService,
    PerformanceUnavailable,
)
from avd.performance.voice import (
    SingingVoiceError,
    SingingVoiceModelVersion,
    VoiceModelImport,
    import_voice_model,
    list_voice_reference_samples,
)

if TYPE_CHECKING:
    from avd.runtime import Runtime


def install_performance_routes(app: FastAPI, runtime_getter: Callable[[], Runtime]) -> None:
    def model_data(model: SingingVoiceModelVersion) -> dict[str, Any]:
        return {
            key: getattr(model, key)
            for key in (
                "id",
                "voice_id",
                "number",
                "engine",
                "model_path",
                "index_path",
                "config_path",
                "reference_path",
                "reference_sha256",
                "sample_rate",
                "parameters",
                "provenance",
                "fingerprint",
                "created_at",
            )
        }

    @app.get("/singing-models")
    def models(voice_id: str | None = None) -> dict[str, Any]:
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            query = select(SingingVoiceModelVersion).order_by(SingingVoiceModelVersion.created_at)
            if voice_id is not None:
                query = query.where(SingingVoiceModelVersion.voice_id == voice_id)
            return {"models": [model_data(model) for model in session.scalars(query)]}

    @app.post("/singing-models/import", status_code=201)
    def import_model(body: VoiceModelImport) -> dict[str, Any]:
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            try:
                model = import_voice_model(
                    session,
                    runtime.settings.data_dir,
                    body,
                    seed_installation=runtime.settings.seed_vc_installation,
                )
            except SingingVoiceError as exc:
                raise HTTPException(422, str(exc)) from exc
            session.commit()
            return model_data(model)

    @app.get("/voices/{voice_id}/singing-reference-samples")
    def voice_reference_samples(voice_id: str) -> dict[str, Any]:
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            try:
                return list_voice_reference_samples(session, runtime.settings.data_dir, voice_id)
            except SingingVoiceError as exc:
                raise HTTPException(422, str(exc)) from exc

    def model_readiness(version_id: str) -> dict[str, Any]:
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            return persisted_voice_readiness(
                session,
                runtime.settings.data_dir,
                version_id,
                installation=runtime.settings.rvc_installation,
                seed_installation=runtime.settings.seed_vc_installation,
            ).model_dump(mode="json")

    @app.get("/singing-models/{version_id}/readiness")
    def get_model_readiness(version_id: str) -> dict[str, Any]:
        return model_readiness(version_id)

    @app.post("/singing-models/{version_id}/readiness", status_code=202)
    def probe_model_readiness(version_id: str, probe: bool = True) -> dict[str, Any]:
        if not probe:
            return model_readiness(version_id)
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            if session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            model = session.get(SingingVoiceModelVersion, version_id)
            if model is None:
                raise HTTPException(404, "Singing model version not found")
            captured = runtime.settings.rvc_installation
            payload = {
                "version_id": version_id,
                "rvc_installation": captured.model_dump(mode="json") if captured else None,
            }
            if model.engine == "seed-vc":
                seed = runtime.settings.seed_vc_installation
                payload = {
                    "version_id": version_id,
                    "seed_vc_installation": seed.model_dump(mode="json") if seed else None,
                }
            existing = next(
                (
                    job
                    for job in session.scalars(
                        select(Job).where(
                            Job.job_type == "singing_model_probe",
                            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
                        )
                    )
                    if probe_payload_matches(job.payload, payload)
                ),
                None,
            )
            job_id = (
                existing.id
                if existing
                else runtime.queue.submit(
                    "gpu",
                    "singing_model_probe",
                    payload,
                    model_key=f"{model.engine}:{version_id}",
                    session=session,
                )
            )
            session.commit()
            return {
                "job_id": job_id,
                "status": existing.status.value if existing else "queued",
                "probe_queued": True,
                "ready_for_inference": False,
                "quality_verified": False,
                "reasons": ["Queued in the shared GPU lane; wait for the model probe result"],
            }

    @app.get("/singing-models/{version_id}/readiness/probes/{job_id}")
    def get_probe(version_id: str, job_id: str) -> dict[str, Any]:
        runtime = runtime_getter()
        with runtime.session_factory() as session:
            job = session.get(Job, job_id)
            if (
                job is None
                or job.job_type != "singing_model_probe"
                or job.payload.get("version_id") != version_id
            ):
                raise HTTPException(404, "Model probe job not found")
            return {
                "job_id": job.id,
                "status": job.status.value,
                "error": job.error,
                "readiness": (job.result or {}).get("readiness"),
            }

    def operation(
        name: str, project_id: str, body: PerformanceConfigPatch | None = None
    ) -> dict[str, Any]:
        service = PerformanceService(runtime_getter(), motion_readiness=motion_workflow_readiness)
        try:
            if name == "configure":
                assert body is not None
                return service.configure(project_id, body)
            return cast(dict[str, Any], getattr(service, name)(project_id))
        except PerformanceUnavailable as exc:
            raise HTTPException(409, exc.detail) from exc

    @app.get("/projects/{project_id}/performance")
    def get_config(project_id: str) -> dict[str, Any]:
        return operation("get_config", project_id)

    @app.put("/projects/{project_id}/performance")
    @app.patch("/projects/{project_id}/performance")
    def configure(project_id: str, body: PerformanceConfigPatch) -> dict[str, Any]:
        return operation("configure", project_id, body)

    @app.post("/projects/{project_id}/performance/prepare", status_code=202)
    def prepare(project_id: str) -> dict[str, Any]:
        return operation("prepare", project_id)

    @app.get("/projects/{project_id}/performance/readiness")
    def readiness(project_id: str) -> dict[str, Any]:
        return operation("readiness", project_id)

    @app.post("/projects/{project_id}/performance/preview", status_code=202)
    def preview(project_id: str) -> dict[str, Any]:
        return operation("preview", project_id)

    @app.post("/projects/{project_id}/performance/video-preview", status_code=202)
    def video_preview(project_id: str) -> dict[str, Any]:
        return operation("video_preview", project_id)

    @app.post("/projects/{project_id}/performance/segment-plan")
    def segment_plan(project_id: str, body: SegmentPlanRequest) -> dict[str, Any]:
        try:
            return PerformanceService(runtime_getter()).segment_plan(project_id, body)
        except PerformanceUnavailable as exc:
            raise HTTPException(409, exc.detail) from exc

    @app.post("/projects/{project_id}/performance/generate", status_code=202)
    def generate(project_id: str) -> dict[str, Any]:
        return operation("generate", project_id)
