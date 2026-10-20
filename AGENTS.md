# AGENTS.md — rules for coding agents in this repo

Read `SPEC.md` first. It is the source of truth for architecture and data model.
If a task conflicts with SPEC.md, stop and say so instead of guessing.

## Layout

```
src/avd/
  state/      # SQLAlchemy models, repository, versioning, dependency graph
  assets/     # character & voice library
  jobs/       # persistent job queue
  backends/   # comfyui client, image API, ffmpeg wrappers
  llm/        # model routing, cost ledger
  agents/     # analysis, script, QA sub-agents
  director/   # director agent + tool definitions
  api/        # FastAPI app
workflows/    # ComfyUI workflow JSON templates
tests/        # pytest, mirrors src layout
```

## Conventions

- Python 3.12, type hints everywhere, Pydantic v2 for I/O schemas, SQLAlchemy 2.0 style.
- Package name `avd`, `src/` layout, `pyproject.toml` (setuptools), deps pinned with `>=`.
- No network calls in tests. External services (LLMs, ComfyUI, image APIs) get a fake/stub.
- Media files live under a configurable `data_dir`; DB stores relative paths only.
- Versions are immutable: never modify or delete a `Version` row or its files.
- Keep functions small; deterministic steps are plain functions, not agents.
- Secrets come from environment variables (`.env`, never committed).

## Done means

- `python -m pytest -q` passes.
- New behavior has tests.
- Finish your turn with a short report: files changed, how to run, anything left open.

## Do not

- Do not add features beyond the current task.
- Do not touch files outside this repo.
- Do not commit secrets, model weights, or media files.
