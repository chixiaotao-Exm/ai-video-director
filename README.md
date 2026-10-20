# AI Video Director

Stateful project state for an AI video director, including immutable versions and
incremental dirty propagation through a per-shot dependency graph.

## Setup

From the repository root on Python 3.12:

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -e ".[dev]"

## Tests

    python -m pytest -q

The default database is created under data/; tests can use an in-memory SQLite
database by passing sqlite+pysqlite:///:memory: to the database helpers.
