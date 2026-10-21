"""Observe native model dispatch without treating injected backends as execution.

The runtime binds a database observer inside its worker process. These helpers
contain no application/config imports, so model adapters can use them safely.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any, cast

ExecutionObserver = Callable[[dict[str, Any]], None]
_OBSERVER: ContextVar[ExecutionObserver | None] = ContextVar(
    "native_execution_observer", default=None
)
_DISPATCH: ContextVar[dict[str, Any] | None] = ContextVar("native_model_dispatch", default=None)


@contextmanager
def native_execution_observer(observer: ExecutionObserver) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def observe_execution(
    *,
    role: str,
    provider: str,
    model: str,
    phase: str,
    details: Mapping[str, Any],
    source: str = "actual",
) -> None:
    observer = _OBSERVER.get()
    if observer is not None:
        observer(
            {
                "role": role,
                "provider": provider,
                "model": model,
                "phase": phase,
                "details": deepcopy(dict(details)),
                "source": source,
            }
        )


@contextmanager
def native_model_dispatch(identity: dict[str, Any]) -> Iterator[None]:
    """Give the real HTTP client frozen model metadata; setting it emits nothing."""
    token = _DISPATCH.set(deepcopy(identity))
    try:
        yield
    finally:
        _DISPATCH.reset(token)


def observe_model_dispatch() -> None:
    """Called only by the native ComfyUI submit implementation before HTTP dispatch."""
    identity = _DISPATCH.get()
    if identity is not None:
        observe_execution(**identity)


def run_model_process(
    arguments: list[str],
    *,
    role: str,
    model: str,
    phase: str,
    details: Mapping[str, Any],
    timeout: float,
    check: bool = True,
    capture_output: bool = True,
    text: bool = True,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Record after Popen succeeds, retaining failed/timed-out launch evidence.

    An explicitly injected runner bypasses this function. With no bound runtime
    observer the ordinary subprocess contract stays unchanged for standalone use.
    """
    if not capture_output or not text:
        raise ValueError("Observed model processes require captured text output")
    if _OBSERVER.get() is None:
        return cast(
            subprocess.CompletedProcess[str],
            subprocess.run(
                arguments, capture_output=True, text=True, check=check, timeout=timeout, **kwargs
            ),
        )
    with subprocess.Popen(
        arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs
    ) as process:
        try:
            observe_execution(
                role=role,
                provider="local",
                model=model,
                phase=phase,
                details={**details, "process_id": process.pid, "evidence": "process_started"},
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as error:
                process.kill()
                stdout, stderr = process.communicate()
                error.output, error.stderr = stdout, stderr
                raise
        except BaseException:
            # A lost claim or failed persistence cannot leave an unobserved model
            # running. ProcessRunner still owns the enclosing process tree.
            if process.poll() is None:
                process.kill()
            process.communicate()
            raise
        if check and process.returncode:
            raise subprocess.CalledProcessError(
                process.returncode, arguments, output=stdout, stderr=stderr
            )
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
