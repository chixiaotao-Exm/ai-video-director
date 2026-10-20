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

## Optional analysis tools

Install optional analysis dependencies with `python -m pip install -e ".[analysis,llm]"`.
FFmpeg and ffprobe must be on PATH for media operations. On Windows, install FFmpeg
(e.g. `winget install Gyan.FFmpeg`) and open a new terminal; on Ubuntu use
`sudo apt-get install ffmpeg`. Check `ffmpeg -version` and `ffprobe -version`.
Tests for real media generation skip when these tools are unavailable; all service
calls in tests use fakes. Configure `AVD_GEMINI_MODEL` explicitly before live analysis.
