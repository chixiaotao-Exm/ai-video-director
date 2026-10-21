"""Keep the GPU lane occupied when a remote performance prompt may still run."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from avd.config import ComfyUIProfile
from avd.jobs.models import Job
from avd.jobs.process import ProcessCleanupError

_REMOTE_STAGES = {"motion_video", "performance_lipsync"}
_ACTIVE = {"running", "pending", "executing", "queued"}
_TERMINAL = {
    "success",
    "complete",
    "completed",
    "error",
    "failed",
    "failure",
    "interrupted",
    "cancelled",
    "canceled",
}


def _terminal(record: Any) -> bool:
    if not isinstance(record, Mapping):
        return False
    status = record.get("status")
    if not isinstance(status, Mapping):
        return False
    state = str(status.get("status_str") or "").lower()
    return state not in _ACTIVE and (status.get("completed") is True or state in _TERMINAL)


def _queue_contains(payload: Any, prompt_id: str) -> bool:
    if not isinstance(payload, dict):
        raise ValueError("ComfyUI queue response is not an object")
    ids = set()
    for key in ("queue_running", "queue_pending"):
        entries = payload.get(key)
        if not isinstance(entries, list):
            raise ValueError("ComfyUI queue response is missing running/pending lists")
        for entry in entries:
            if not isinstance(entry, list) or len(entry) < 2 or not isinstance(entry[1], str):
                raise ValueError("ComfyUI queue response has an invalid prompt entry")
            ids.add(entry[1])
    return prompt_id in ids


def verify_remote_release(job: Job, *, transport: httpx.BaseTransport | None = None) -> None:
    """Run in the supervising parent before failing an isolated remote job.

    The child writes its journal before dispatch, so its absence proves that no
    prompt was submitted. A known prompt can be released only after the frozen
    server confirms terminal history or absence from both server queues. This
    never interrupts or deletes another user's ComfyUI work.
    """
    if job.job_type not in _REMOTE_STAGES:
        return
    from avd.executors.handlers import _data_path

    try:
        root = Path(job.payload["data_dir"]).expanduser().resolve()
        base = _data_path(root, job.payload["output_dir"])
        if base.relative_to(root).parts[0] != "projects":
            return  # The executor rejects this payload before any submission.
    except (KeyError, TypeError, ValueError):
        return
    journal = base / "remote-submission.json"
    if not journal.exists():
        return
    server, prompt_id = "unknown frozen server", None
    try:
        profile = ComfyUIProfile.model_validate(job.payload["recipe"]["comfyui_profile"])
        server = profile.base_url.rstrip("/")
        record = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("stage") != job.job_type:
            raise ValueError("Submission journal has an invalid stage")
        if str(record.get("server") or "").rstrip("/") != server:
            raise ValueError("Submission journal does not match the frozen ComfyUI server")
        prompt_id = record.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ValueError("Submission outcome is unknown; its prompt id was not received")
        host = (urlsplit(server).hostname or "").lower()
        with httpx.Client(
            base_url=server + "/",
            timeout=min(profile.timeout, 4.0),
            transport=transport,
            trust_env=host not in {"localhost", "127.0.0.1", "::1"},
        ) as client:
            response = client.get(f"history/{quote(prompt_id, safe='')}")
            response.raise_for_status()
            history = response.json()
            if not isinstance(history, dict):
                raise ValueError("ComfyUI history response is not an object")
            if _terminal(history.get(prompt_id)):
                return
            response = client.get("queue")
            response.raise_for_status()
            if not _queue_contains(response.json(), prompt_id):
                return
        reason = "The submitted prompt is still running or queued on ComfyUI"
    except (OSError, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        reason = f"Remote termination could not be verified ({type(exc).__name__}: {exc})"
    identity = f"prompt {prompt_id!r}" if prompt_id else "a prompt with an unknown id"
    raise ProcessCleanupError(
        f"{reason}. GPU lane held for {identity} on {server}. "
        f"Inspect the durable journal {journal} and that server's /queue and /history. "
        "Wait for this prompt to finish or stop only this prompt; do not interrupt other work. "
        "Only after confirming no associated remote execution remains, stop all AVD workers "
        "and run avd worker --lane gpu --recover-stale --once for exclusive recovery."
    )
