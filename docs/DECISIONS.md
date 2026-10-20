# Decisions

This file records implementation choices made while completing the milestone plan.

## T01

- **Date:** 2026-10-20
- **Decision:** Shot indexes are 1-based (`1..N`) because the public workflow refers to “shot 2” and stores a user-facing shot number.
- **Alternatives:** Python-style zero-based indexes; rejected for user-facing state.

## T02

- **Date:** 2026-10-20
- **Decision:** Asset references are copied into content-addressed `data/assets/...` storage and only POSIX paths relative to `data_dir` are persisted.
- **Alternatives:** Preserve source absolute paths; rejected because project state must be portable.

## T03

- **Date:** 2026-10-20
- **Decision:** SQLite queue claims use `BEGIN IMMEDIATE`; GPU worker execution is guarded by one process-wide lock.
- **Alternatives:** Optimistic row updates only; rejected because the requested worker behavior is serial GPU execution.
## T03b

- **Date:** 2026-10-20
- **Decision:** A crashed running job at its attempt limit is terminally failed with the deterministic error `worker crashed`; otherwise it is requeued.
- **Alternatives:** Always requeue stale jobs; rejected because that can retry indefinitely after repeated crashes.
- **Decision:** `on_finished` callback exceptions are logged and swallowed after job state is persisted.
- **Alternatives:** Let callbacks terminate the worker; rejected because notifications are non-critical to job execution.
