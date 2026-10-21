"""HTTP client for a ComfyUI server."""

from __future__ import annotations

import json
import mimetypes
import time
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from avd.jobs.native import observe_model_dispatch


class ComfyUIError(RuntimeError):
    """Base class for ComfyUI client failures."""


class ComfyUIHTTPError(ComfyUIError):
    """The ComfyUI server returned an unsuccessful HTTP response."""

    def __init__(self, status_code: int, message: str, *, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class ComfyUIProtocolError(ComfyUIError):
    """The server response did not match the ComfyUI API shape."""


class ComfyUINodeError(ComfyUIError):
    """ComfyUI accepted a prompt but reported a node/execution error."""

    def __init__(self, message: str, *, node_errors: Any = None) -> None:
        super().__init__(message)
        self.node_errors = node_errors


class ComfyUITimeoutError(ComfyUIError, TimeoutError):
    """A prompt did not finish before the requested timeout."""


class ComfyUIClient:
    """Small synchronous client for the ComfyUI HTTP API.

    ``client`` is injectable so tests can use :class:`httpx.MockTransport`.  The
    default client is owned by this object and is closed by ``close`` or the
    context manager methods.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 60.0,
        *,
        client_id: str | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = str(base_url).strip().rstrip("/")
        if not normalized:
            raise ValueError("base_url must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        self.base_url = normalized
        self.timeout = timeout
        self.client_id = client_id or uuid.uuid4().hex
        self._owns_client = client is None
        # A local ComfyUI or SSH tunnel must never be sent through a system proxy.
        host = (urlsplit(normalized).hostname or "").lower()
        local = host in {"localhost", "127.0.0.1", "::1"}
        self._client = client or httpx.Client(
            timeout=timeout, transport=transport, trust_env=not local
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "ComfyUIClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upload_file(self, path: str | Path) -> str:
        """Upload an input file and return ComfyUI's server-side file name."""

        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"ComfyUI upload file does not exist: {source}")
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        with source.open("rb") as handle:
            response = self._request(
                "POST",
                "/upload/image",
                files={"image": (source.name, handle, content_type)},
            )
        payload = self._json(response, "upload response")
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            raise ComfyUIProtocolError("ComfyUI upload response did not include a non-empty 'name'")
        return name

    def submit(self, workflow: Mapping[str, Any]) -> str:
        """Submit an API-format workflow and return its prompt id."""

        if not isinstance(workflow, Mapping):
            raise TypeError("workflow must be a mapping")
        observe_model_dispatch()
        response = self._request(
            "POST",
            "/prompt",
            json={"prompt": dict(workflow), "client_id": self.client_id},
        )
        payload = self._json(response, "prompt response")
        node_errors = payload.get("node_errors")
        prompt_error = payload.get("error")
        if node_errors or prompt_error:
            detail = node_errors if node_errors else prompt_error
            raise ComfyUINodeError(self._format_node_errors(detail), node_errors=detail)
        prompt_id = payload.get("prompt_id")
        if not isinstance(prompt_id, str) or not prompt_id:
            raise ComfyUIProtocolError(
                "ComfyUI prompt response did not include a non-empty 'prompt_id'"
            )
        return prompt_id

    def wait(
        self,
        prompt_id: str,
        poll_interval: float = 0.25,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Poll history until a prompt completes and return its history record."""

        if not prompt_id:
            raise ValueError("prompt_id must not be empty")
        if poll_interval < 0:
            raise ValueError("poll_interval must be non-negative")
        effective_timeout = float(self.timeout if timeout is None else timeout)
        if effective_timeout <= 0:
            raise ValueError("timeout must be positive")
        started = time.monotonic()
        while True:
            record = self._history_record(prompt_id)
            if record is not None:
                self._raise_for_history_errors(record)
                if self._history_is_complete(record):
                    return record
            elapsed = time.monotonic() - started
            if elapsed >= effective_timeout:
                raise ComfyUITimeoutError(
                    f"Timed out waiting for ComfyUI prompt {prompt_id!r} "
                    f"after {effective_timeout:g}s"
                )
            time.sleep(min(poll_interval, effective_timeout - elapsed))

    def download_outputs(
        self,
        prompt_id: str,
        dest_dir: str | Path,
    ) -> list[Path]:
        """Download all output files recorded for a completed prompt."""

        if not prompt_id:
            raise ValueError("prompt_id must not be empty")
        record = self._history_record(prompt_id)
        if record is None:
            raise ComfyUIProtocolError(f"ComfyUI has no history for prompt {prompt_id!r}")
        self._raise_for_history_errors(record)
        outputs = record.get("outputs")
        if not isinstance(outputs, Mapping):
            raise ComfyUIProtocolError(
                f"ComfyUI history for prompt {prompt_id!r} has no 'outputs' mapping"
            )

        destination = Path(dest_dir).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        used_names = {path.name for path in destination.iterdir()}
        for node_outputs in outputs.values():
            for output in self._iter_output_records(node_outputs):
                filename = output.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                basename = Path(filename).name
                if not basename or basename in {".", ".."}:
                    continue
                final_name = self._unique_name(basename, used_names)
                used_names.add(final_name)
                response = self._request(
                    "GET",
                    "/view",
                    params={
                        "filename": filename,
                        "subfolder": str(output.get("subfolder") or ""),
                        "type": str(output.get("type") or "output"),
                    },
                )
                target = destination / final_name
                target.write_bytes(response.content)
                downloaded.append(target)
        return downloaded

    def _history_record(self, prompt_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/history/{quote(prompt_id, safe='')}")
        payload = self._json(response, "history response")
        if not isinstance(payload, Mapping):
            raise ComfyUIProtocolError("ComfyUI history response must be a JSON object")
        record = payload.get(prompt_id)
        if record is None:
            return None
        if not isinstance(record, Mapping):
            raise ComfyUIProtocolError(
                f"ComfyUI history record for prompt {prompt_id!r} must be an object"
            )
        return dict(record)

    @staticmethod
    def _history_is_complete(record: Mapping[str, Any]) -> bool:
        status = record.get("status")
        if isinstance(status, Mapping):
            status_string = str(status.get("status_str") or "").lower()
            if status_string in {"running", "pending", "executing", "queued"}:
                return False
            if status.get("completed") is True:
                return True
            if status.get("completed") is False:
                return False
            if status_string in {"success", "complete", "completed"}:
                return True
        return "outputs" in record

    @classmethod
    def _raise_for_history_errors(cls, record: Mapping[str, Any]) -> None:
        node_errors = record.get("node_errors")
        record_error = record.get("error")
        status = record.get("status")
        messages: Iterable[Any] = ()
        if isinstance(status, Mapping):
            raw_messages = status.get("messages")
            if isinstance(raw_messages, list):
                messages = raw_messages
            status_string = str(status.get("status_str") or "").lower()
            if status_string in {"error", "failed", "failure"} and not node_errors:
                node_errors = {"status": status_string, "messages": list(messages)}
        if record_error and not node_errors:
            node_errors = record_error
        for message in messages:
            if isinstance(message, (list, tuple)) and message:
                if str(message[0]).lower() in {
                    "execution_error",
                    "execution_interrupted",
                    "error",
                }:
                    node_errors = node_errors or message
                    break
        if node_errors:
            raise ComfyUINodeError(cls._format_node_errors(node_errors), node_errors=node_errors)

    @staticmethod
    def _format_node_errors(node_errors: Any) -> str:
        try:
            detail = json.dumps(node_errors, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            detail = repr(node_errors)
        return f"ComfyUI workflow node error: {detail}"

    @staticmethod
    def _iter_output_records(value: Any) -> Iterable[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            if "filename" in value:
                yield value
            else:
                for nested in value.values():
                    yield from ComfyUIClient._iter_output_records(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from ComfyUIClient._iter_output_records(nested)

    @staticmethod
    def _unique_name(name: str, used_names: set[str]) -> str:
        if name not in used_names:
            return name
        path = Path(name)
        index = 2
        while True:
            candidate = f"{path.stem}_{index}{path.suffix}"
            if candidate not in used_names:
                return candidate
            index += 1

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise ComfyUIError(f"ComfyUI request {method} {path} failed: {exc}") from exc
        if response.is_error:
            body: Any
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = response.text
            if isinstance(body, Mapping) and body.get("node_errors"):
                raise ComfyUINodeError(
                    self._format_node_errors(body["node_errors"]),
                    node_errors=body["node_errors"],
                )
            raise ComfyUIHTTPError(
                response.status_code,
                f"ComfyUI {method} {path} returned HTTP {response.status_code}: {body}",
                body=body,
            )
        return response

    @staticmethod
    def _json(response: httpx.Response, description: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise ComfyUIProtocolError(f"ComfyUI {description} was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ComfyUIProtocolError(f"ComfyUI {description} must be a JSON object")
        return payload


__all__ = [
    "ComfyUIClient",
    "ComfyUIError",
    "ComfyUIHTTPError",
    "ComfyUINodeError",
    "ComfyUIProtocolError",
    "ComfyUITimeoutError",
]


def comfyui_status(base_url: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Whether a ComfyUI server answers at ``base_url`` (checked before GPU work)."""
    host = (urlsplit(base_url).hostname or "").lower()
    try:
        with httpx.Client(
            timeout=timeout, trust_env=host not in {"localhost", "127.0.0.1", "::1"}
        ) as client:
            response = client.get(f"{base_url.rstrip('/')}/system_stats")
            response.raise_for_status()
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {' '.join(str(exc).split())[:160]}"
