"""Spawned job execution with deadlines and ownership of the entire local process tree."""

from __future__ import annotations

import ctypes
import multiprocessing
import os
import queue
import signal
import threading
import time
import traceback
from collections.abc import Callable
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Protocol

from .models import Job

JobHandler = Callable[[Job], dict[str, Any]]


class JobTimeoutError(TimeoutError):
    """A job exceeded its execution or graceful-shutdown deadline."""


class ProcessCleanupError(RuntimeError):
    """The supervisor cannot confirm that its process tree has stopped."""


class JobRunner(Protocol):
    def run(
        self,
        handler: JobHandler,
        job: Job,
        *,
        timeout: float,
        stop_event: threading.Event | None,
        shutdown_grace: float,
    ) -> dict[str, Any]: ...


def _watch_parent(parent_pid: int, stop_event: threading.Event, interval: float = 0.1) -> None:
    """Detect supervisor loss and stop this orphan's entire POSIX process group."""
    while not stop_event.wait(interval):
        if os.getppid() != parent_pid:
            try:
                getattr(os, "killpg")(os.getpid(), getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
            return


def _execute(connection: Connection, handler: JobHandler, job: Job, parent_pid: int) -> None:
    """Wait for containment before permitting any handler work in the child."""
    watcher = None
    stop_watcher = threading.Event()
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if os.name != "nt":
            getattr(os, "setsid")()
            watcher = threading.Thread(
                target=_watch_parent,
                args=(parent_pid, stop_watcher),
                name=f"job-parent-{job.id}",
                daemon=True,
            )
            watcher.start()
        connection.send(("ready", None))
        if connection.recv() != "run":
            return
        result = handler(job)
        if not isinstance(result, dict):
            raise TypeError("Job handlers must return a dictionary")
        connection.send(("result", result))
    except BaseException:
        try:
            connection.send(("error", traceback.format_exc()))
        except (EOFError, OSError):
            pass
    finally:
        connection.close()
        stop_watcher.set()
        if watcher is not None:
            watcher.join(timeout=0.5)


def _read_messages(connection: Connection, messages: queue.Queue[tuple[str, Any]]) -> None:
    # Reading on a separate thread also keeps the deadline enforceable while a
    # child is serializing/sending a large or incomplete response.
    try:
        while True:
            messages.put(connection.recv())
    except (EOFError, OSError):
        messages.put(("closed", None))


class _WindowsJob:
    """A parent-owned Job Object kills children and grandchildren together.

    The kill-on-close limit also applies if the supervising worker crashes.
    A child waits for the supervisor's handshake before executing its handler,
    so it cannot spawn subprocesses before joining this Job Object.
    """

    def __init__(self, process: BaseProcess) -> None:
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_uint64)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self.kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise getattr(ctypes, "WinError")()
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise getattr(ctypes, "WinError")()
            if not self.kernel.AssignProcessToJobObject(self.handle, process.sentinel):
                raise getattr(ctypes, "WinError")()
        except BaseException:
            self.close()
            raise

    def stop(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise getattr(ctypes, "WinError")()

    def active_count(self) -> int:
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        info = Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise getattr(ctypes, "WinError")()
        return int(info.ActiveProcesses)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _posix_group_active(group_id: int) -> bool:
    try:
        getattr(os, "killpg")(group_id, 0)
    except ProcessLookupError:
        return False
    # Linux may leave orphaned zombies until PID 1 reaps them. They cannot run or
    # write files and must not hold the GPU lane indefinitely. Other POSIX hosts
    # conservatively wait for group disappearance when /proc is unavailable.
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "stat").read_text()
            except (FileNotFoundError, ProcessLookupError):
                continue
            fields = raw.rsplit(")", 1)[1].split()
            if int(fields[2]) == group_id and fields[0] not in {"Z", "X"}:
                return True
    except (OSError, ValueError, IndexError):
        return True
    return False


def _stop_tree(process: BaseProcess, windows_job: _WindowsJob | None, ready: bool) -> None:
    try:
        if windows_job is not None:
            windows_job.stop()
        elif os.name != "nt" and ready:
            assert process.pid is not None
            try:
                getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
            except ProcessLookupError:
                pass
        elif process.is_alive():
            # No handshake means the handler has not started and has no children.
            process.kill()
        process.join(timeout=5)
        if process.is_alive():
            raise ProcessCleanupError("Job process did not exit after termination")
        if windows_job is not None:
            deadline = time.monotonic() + 5
            while windows_job.active_count():
                if time.monotonic() >= deadline:
                    raise ProcessCleanupError("Job descendants did not exit after termination")
                time.sleep(0.02)
        elif os.name != "nt" and ready:
            assert process.pid is not None
            deadline = time.monotonic() + 5
            while _posix_group_active(process.pid):
                if time.monotonic() >= deadline:
                    raise ProcessCleanupError("Job process group still has active descendants")
                time.sleep(0.02)
    except ProcessCleanupError:
        raise
    except Exception as exc:
        raise ProcessCleanupError(f"Could not stop job process tree: {exc}") from exc


class ProcessRunner:
    """Execute pickleable handlers with spawn on both Windows and Linux."""

    def run(
        self,
        handler: JobHandler,
        job: Job,
        *,
        timeout: float,
        stop_event: threading.Event | None,
        shutdown_grace: float,
    ) -> dict[str, Any]:
        context = multiprocessing.get_context("spawn")
        connection, child_connection = context.Pipe()
        process = context.Process(
            target=_execute, args=(child_connection, handler, job, os.getpid())
        )
        windows_job = None
        reader = None
        ready = False
        started = False
        cleaned = False
        deadline = time.monotonic() + timeout
        shutdown_deadline = None
        messages: queue.Queue[tuple[str, Any]] = queue.Queue()
        try:
            process.start()
            started = True
            child_connection.close()
            if os.name == "nt":
                windows_job = _WindowsJob(process)
            reader = threading.Thread(
                target=_read_messages, args=(connection, messages), daemon=True
            )
            reader.start()
            while True:
                now = time.monotonic()
                if stop_event is not None and stop_event.is_set() and shutdown_deadline is None:
                    shutdown_deadline = now + shutdown_grace
                if now >= deadline:
                    raise JobTimeoutError(f"Job {job.job_type} timed out after {timeout:g}s")
                if shutdown_deadline is not None and now >= shutdown_deadline:
                    raise JobTimeoutError(
                        f"Job exceeded worker shutdown grace of {shutdown_grace:g}s"
                    )
                try:
                    kind, value = messages.get(timeout=0.025)
                except queue.Empty:
                    continue
                if kind == "ready":
                    ready = True
                    connection.send("run")
                elif kind == "result":
                    return dict(value)
                elif kind == "error":
                    raise RuntimeError(f"Job process failed:\n{value}")
                elif kind == "closed":
                    raise RuntimeError("Job process exited without returning a result")
        finally:
            try:
                if started:
                    _stop_tree(process, windows_job, ready)
                cleaned = True
            finally:
                child_connection.close()
                connection.close()
                if reader is not None:
                    reader.join(timeout=1)
                if windows_job is not None:
                    windows_job.close()
                if started and cleaned:
                    process.close()


__all__ = ["JobRunner", "JobTimeoutError", "ProcessCleanupError", "ProcessRunner"]
