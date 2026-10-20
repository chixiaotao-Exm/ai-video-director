# Progress

## T01 — project state

- Implemented SQLAlchemy project state, dependency graph, versioning, dirty propagation, and tests.
- Commit: `485b0a6`.
- Baseline test suite: 11 passed.

## T02 — asset library

- Implemented content-addressed character and voice storage, consent fields, and asset library.
- Commit: `27c0de6`.
- Baseline test suite: 19 passed.

## T02b — consent review fixes

- Closed the project asset ID bypass, moved consent logic to `state.consent`, and required voice samples.
- Commit: `9f08df3`.
- Baseline test suite: 21 passed.

## T03 — persistent job queue

- Implemented durable jobs, atomic claiming, retries, recovery, and workers.
- Commit: `4dde013`.
- Baseline test suite: 30 passed.
## T03b — queue review fixes

- Fixed stale job recovery to fail exhausted attempts with `worker crashed` and requeue only retryable jobs.
- Logged and swallowed `on_finished` callback exceptions so worker loops continue.
- Tests: 32 passed.
