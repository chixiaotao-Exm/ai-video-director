"""Database engine and session helpers for project state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def default_database_url(data_dir: str | os.PathLike[str] | None = None) -> str:
    """Return the default SQLite URL, creating its parent directory if needed."""

    root = Path(data_dir or os.environ.get("AVD_DATA_DIR", "data"))
    root.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(root / 'state.db').resolve().as_posix()}"


def create_engine(
    db_url: str | None = None,
    *,
    data_dir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> Engine:
    """Create a SQLAlchemy engine.

    ``sqlite:///:memory:`` is suitable for tests.  File-backed SQLite databases
    use ``check_same_thread=False`` so a session can be used by the app's worker
    threads when needed.
    """

    url = db_url or default_database_url(data_dir)
    engine_kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        connect_args = dict(kwargs.pop("connect_args", {}))
        connect_args.setdefault("check_same_thread", False)
        connect_args.setdefault("timeout", 30)
        engine_kwargs["connect_args"] = connect_args
    engine_kwargs.update(kwargs)
    engine = sqlalchemy_create_engine(url, **engine_kwargs)
    _enable_foreign_keys(engine)
    return engine


def _sqlite_foreign_keys(connection: Any, record: Any) -> None:
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _enable_foreign_keys(engine: Engine) -> None:
    if engine.dialect.name == "sqlite" and not event.contains(
        engine, "connect", _sqlite_foreign_keys
    ):
        event.listen(engine, "connect", _sqlite_foreign_keys)


def create_session_factory(
    engine: Engine | str | None = None,
    *,
    db_url: str | None = None,
    data_dir: str | os.PathLike[str] | None = None,
    **kwargs: Any,
) -> sessionmaker[Session]:
    """Create a typed session factory from an engine or database URL."""

    if isinstance(engine, str):
        if db_url is not None:
            raise TypeError("pass either an engine/URL positional argument or db_url")
        bound_engine = create_engine(engine, data_dir=data_dir, **kwargs)
    else:
        bound_engine = engine or create_engine(db_url, data_dir=data_dir, **kwargs)
    return sessionmaker(bind=bound_engine, class_=Session, expire_on_commit=False)


def init_db(engine: Engine) -> None:
    """Create registered tables; the application registers its own model modules."""

    _enable_foreign_keys(engine)
    Base.metadata.create_all(engine)
    upgrade_sqlite_schema(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            # Also cover engines supplied by callers with an existing connection.
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            for action in ("UPDATE", "DELETE"):
                connection.exec_driver_sql(
                    f"CREATE TRIGGER IF NOT EXISTS versions_no_{action.lower()} "
                    f"BEFORE {action} ON versions BEGIN "
                    "SELECT RAISE(ABORT, 'Version rows are immutable'); END"
                )


def upgrade_sqlite_schema(engine: Engine) -> None:
    """Add the nullable audit/worker fields introduced after the initial schema."""

    if engine.dialect.name != "sqlite":
        return
    additions = {
        "characters": {
            "consent_confirmed_by": "VARCHAR(255)",
            "consent_confirmed_at": "DATETIME",
        },
        "voices": {
            "consent_confirmed_by": "VARCHAR(255)",
            "consent_confirmed_at": "DATETIME",
        },
        "jobs": {"claim_token": "VARCHAR(36)", "lease_expires_at": "DATETIME"},
        "cost_entries": {
            "job_id": "VARCHAR(36) REFERENCES jobs(id) ON DELETE SET NULL",
            "attempt": "INTEGER",
        },
    }
    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}'
                    )
        if "cost_entries" in tables:
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_cost_entries_job_attempt "
                "ON cost_entries (job_id, attempt)"
            )


# Common aliases keep the helper convenient for app code and tests.
get_engine = create_engine
get_session_factory = create_session_factory


__all__ = [
    "create_engine",
    "create_session_factory",
    "default_database_url",
    "get_engine",
    "get_session_factory",
    "init_db",
    "upgrade_sqlite_schema",
]
