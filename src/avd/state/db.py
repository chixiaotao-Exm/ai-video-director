"""Database engine and session helpers for project state."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine as sqlalchemy_create_engine
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
    return sqlalchemy_create_engine(url, **engine_kwargs)


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
    """Create all state tables for a new database."""

    # Importing the job model registers it on the shared declarative metadata.
    from avd.jobs.models import Job  # noqa: F401
    from avd.director.models import ChatMessage  # noqa: F401
    from avd.director.review import Review  # noqa: F401
    from avd.llm.ledger import CostEntry  # noqa: F401

    Base.metadata.create_all(engine)


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
]
