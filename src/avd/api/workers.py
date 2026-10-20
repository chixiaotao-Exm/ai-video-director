"""Worker controls for processes owned by this server."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request

from avd.jobs.supervisor import WorkerSupervisor

WorkerLane = Literal["gpu", "api", "cpu", "all"]


def install_worker_routes(app: FastAPI) -> None:
    def supervisor(request: Request) -> WorkerSupervisor:
        value = getattr(request.app.state, "worker_supervisor", None)
        if not isinstance(value, WorkerSupervisor):
            raise HTTPException(503, "Worker supervisor is unavailable")
        return value

    @app.get("/workers")
    def workers(request: Request) -> dict[str, Any]:
        return supervisor(request).status()

    @app.post("/workers/{lane}/start")
    def start_worker(lane: WorkerLane, request: Request) -> dict[str, Any]:
        try:
            return supervisor(request).start(lane)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/workers/{lane}/stop")
    def stop_worker(lane: WorkerLane, request: Request) -> dict[str, Any]:
        return supervisor(request).stop(lane)


__all__ = ["install_worker_routes"]
