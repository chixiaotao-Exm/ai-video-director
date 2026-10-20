"""One director turn per project at a time, across the API server and worker processes.

Proactive reports (job finished, sub-agent report) and the user's own messages can
arrive together; interleaving two turns would scramble tool calls and results.
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class TurnBusy(TimeoutError):
    """Another director turn for this project did not finish in time."""


@contextmanager
def director_turn(data_dir: Path, project_id: str, timeout: float = 600.0) -> Iterator[None]:
    folder = data_dir.expanduser().resolve() / ".locks"
    folder.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(f"director\0{project_id}".encode()).hexdigest()[:32]
    stream = (folder / f"{name}.lock").open("a+b")
    deadline = time.monotonic() + timeout
    try:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        while True:
            try:
                stream.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TurnBusy("The director is still busy with this project") from None
                time.sleep(0.25)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


__all__ = ["TurnBusy", "director_turn"]
