"""Project spending checks and post-call accounting."""

from __future__ import annotations

import errno
import hashlib
import math
import os
import sys
import threading
import weakref
from _thread import LockType
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import event, inspect, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, SessionTransaction, object_session

from avd.state.models import Project


class BudgetExceeded(RuntimeError):
    """The requested action would exceed the remaining project budget."""


class BudgetBusy(BudgetExceeded):
    """Another transaction is reserving or spending this project's budget."""


class ConfirmationRequired(RuntimeError):
    """The action exceeds the configured threshold for user confirmation."""


_MEMORY_LOCKS: weakref.WeakKeyDictionary[Engine, dict[str, LockType]] = weakref.WeakKeyDictionary()
_MEMORY_LOCKS_GUARD = threading.Lock()
_LOCKS_KEY = "avd_project_budget_locks"
_LISTENER_KEY = "avd_project_budget_listener"


def _acquire_lock(session: Session, project_id: str) -> Callable[[], None]:
    engine = session.get_bind(mapper=Project).engine
    database = engine.url.database
    if (
        engine.dialect.name != "sqlite"
        or not database
        or database == ":memory:"
        or engine.url.query.get("mode") == "memory"
    ):
        # In-memory databases cannot be shared between independent processes.
        with _MEMORY_LOCKS_GUARD:
            locks = _MEMORY_LOCKS.setdefault(engine, {})
            lock = locks.setdefault(project_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise BudgetBusy("Another budget operation is active for this project; try again.")
        return lock.release

    path = Path(database).expanduser().resolve()
    directory = path.parent / ".avd-budget-locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{os.path.normcase(str(path))}\0{project_id}".encode()).hexdigest()
    stream = (directory / f"{key}.lock").open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            raise BudgetBusy(
                "Another budget operation is active for this project; try again."
            ) from None
        raise

    def release() -> None:
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    return release


def _release_transaction_locks(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        for release in session.info.pop(_LOCKS_KEY, {}).values():
            release()


def acquire_project_budget(project: Project) -> None:
    """Serialize budget decisions until their caller commits or rolls back.

    A file-backed SQLite database uses an OS lock shared by every worker. The
    lock is nonblocking: a scheduler may already hold SQLite's write lock, so
    waiting here could deadlock a paid call trying to publish its cost. Keeping
    the lock through the outer transaction prevents another call from spending
    money before this call's ledger entry or queued reservation is durable.
    """
    session = object_session(project)
    state = inspect(project)
    if session is None or not state.persistent:
        return
    locks = session.info.setdefault(_LOCKS_KEY, {})
    if project.id not in locks:
        locks[project.id] = _acquire_lock(session, project.id)
    if not session.info.get(_LISTENER_KEY):
        event.listen(session, "after_transaction_end", _release_transaction_locks)
        session.info[_LISTENER_KEY] = True
    # Retain intentional uncommitted edits, but never check a stale loaded spend.
    fields = [
        name for name in ("spent", "budget_limit") if not state.attrs[name].history.has_changes()
    ]
    if fields:
        with session.no_autoflush:
            session.refresh(project, fields)


def _validate_cost(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Cost must be finite and non-negative")


class BudgetGuard:
    def __init__(self, confirm_threshold: float = 1.0) -> None:
        _validate_cost(confirm_threshold)
        self.confirm_threshold = confirm_threshold

    def check(
        self,
        project: Project,
        estimated_cost: float,
        *,
        confirmed: bool = False,
    ) -> None:
        """Validate without spending. Confirmation never overrides the project limit."""

        _validate_cost(estimated_cost)
        acquire_project_budget(project)
        spent = project.spent or 0.0
        if project.budget_limit is not None and spent + estimated_cost > project.budget_limit:
            raise BudgetExceeded(
                f"Estimated cost ${estimated_cost:.6f} exceeds project remaining "
                f"budget ${project.budget_limit - spent:.6f}"
            )
        if estimated_cost > self.confirm_threshold and not confirmed:
            raise ConfirmationRequired(
                f"Estimated cost ${estimated_cost:.6f} exceeds confirmation "
                f"threshold ${self.confirm_threshold:.6f}"
            )

    def record_spend(self, project: Project, actual_cost: float) -> None:
        """Accumulate completed work; the owning transaction commits the change."""

        _validate_cost(actual_cost)
        session = object_session(project)
        if session is None:
            project.spent = (project.spent or 0.0) + actual_cost
            return
        # Completion accounting must not remove an outstanding reservation while
        # another transaction is deciding against an earlier spent snapshot.
        acquire_project_budget(project)
        session.flush()
        session.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(spent=Project.spent + actual_cost)
            .execution_options(synchronize_session="fetch")
        )


__all__ = [
    "BudgetBusy",
    "BudgetExceeded",
    "BudgetGuard",
    "ConfirmationRequired",
    "acquire_project_budget",
]
