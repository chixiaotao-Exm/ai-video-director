# SPEC review — 2026-10-20

This review covers SPEC.md sections 1–9 and completed milestones through T15. T16 observability and robustness remains in progress; PROGRESS.md preserves the validation result for each milestone.

## Fixed findings

| Finding | Change and verification |
| --- | --- |
| Version rows could be deleted or changed with bulk SQL/cascades | ORM deletion protection, SQLite UPDATE/DELETE triggers, foreign keys and tests for raw SQL/parent deletion. Existing versions and files are preserved. |
| Paths could be absolute or traverse directories | Portable Windows/Unix validation at state flush and API/storage boundaries; tests include drive/UNC/traversal paths. |
| Asset storage used a racy exists-then-copy operation | Hash captured bytes once, publish exclusively with a hard link, verify collisions, constrain asset IDs; concurrent/source-change tests. |
| Character prompt and asset replacement did not update existing recipes | Materialized frame/voice recipes are updated and appropriate dependents dirtied; old versions remain unchanged. |
| Revert left script, voice or analysis mirrors inconsistent | Script reverts synchronize per-shot recipes; voice reverts update readable script snapshots; analysis snapshots store their template. Legacy analysis versions without an embedded template clear the mirror instead of showing incorrect content. |
| GPU serial execution worked only within one process | SQLite atomically checks lane occupancy; independent-process tests prove only one running GPU job. |
| Crash recovery could reset live jobs and accept late completions | Persisted renewable leases and claim tokens; recover only expired leases unless explicit force; workers discard results from obsolete attempts. |
| Jobs returned detached expired attributes with default SQLAlchemy sessions | Flush/refresh/detach while transaction is held, then commit; default expire-on-commit regression tests. |
| Cancellation left nodes stuck and exhausted jobs could not be retried | Reconciliation releases cancelled nodes; explicit retry preserves attempt history and obtains fresh scheduling/budget checks. UI continue and director rebuild can request failed-node retry. |
| Restart publication was rolled back by a later budget refusal | Finished-job reconciliation commits separately before planning. Tests cover budget and consent refusals after successful work. |
| Cache/execution ignored changed model or workflow settings | Effective recipes capture image model and actual workflow JSON/profile. Workers execute the captured configuration; tests change current settings after enqueue. |
| Failed paid attempts and malformed tool responses lost costs | Per-job/attempt ledger uniqueness, paid-boundary markers and failure accounting; malformed OpenAI tool JSON carries usage into the ledger before raising. Replay cannot charge twice. |
| Budget approval was a persisted blanket boolean | Runtime rechecks persisted approval fingerprint, amount, spend and pending reservations before each LLM call/repair and media execution; approved director chat now resumes correctly. |
| Async continuation could outlive human approval | Runtime scheduling/execution hooks recheck current reviews, asset consent and captured recipe freshness. |
| Paid script versions recorded zero cost | Authoring runtime attributes its script ledger entries to the script version without charging twice. |
| Image generation was outside the model routing layer | ImageRouter is the image-model entry point; backend adapters remain replaceable. |
| Character sheet generation had no creation/review path | Prompt-only fictional sheets are queued through API/director/UI, persisted as unconsented assets, previewed and explicitly confirmed by the human. Recovery reuses an already-created sheet. Reference edits continue to require consented assets. |
| Smoke scripts inconsistently read .env and accepted empty outputs | Settings covers all smoke variables; ComfyUI requires artifacts; image smoke has a private durable ledger; analysis checks its configured route. |
| Missing static/code quality gates | Ruff lint+format and mypy, strict for state/jobs/engine and normal elsewhere; scripts/check.ps1 and check.sh run the whole gate. |

## SPEC coverage

- **§1 architecture:** state is shared truth; director alone talks to the user; script/analysis return structured data; deterministic execution stays ordinary Python. Provider and image routing are centralized. The `qa` role is available; automatic QA scoring beyond the requested tools is not exposed as a separate workflow.
- **§2 state:** global assets, project/node/version fields, dependency association table, deterministic plans, dirty propagation, immutable snapshots, dependency-aware cache and reverts have regression tests.
- **§3 queue:** SQLite durability, atomic claims, GPU grouping/serialization, API concurrency, crash recovery and explicit retries have offline tests. SQLite is the supported concurrent queue database; other dialects are not claimed as supported.
- **§4 ComfyUI:** profile switching, typed templates and HTTP lifecycle/error tests are implemented. Shipped templates are intentionally non-runnable contracts awaiting actual exports.
- **§5 tools:** all 17 schemas and project ownership checks are tested. Long authoring and generation return queue IDs.
- **§6 workflow:** separate structure review followed by script/frames/final checkpoints; human consent and bounded budget confirmation are separate from model tools. UI labels now preserve the three numbered checkpoints.
- **§7 compliance:** unconsented face/voice assets are rejected; structure-only prompts exclude reference identity/verbatim copying; final FFmpeg graph includes a visible AI-generated label and metadata comment.
- **§8 stack:** Python 3.12, SQLAlchemy/Pydantic 2, optional SDK/analysis/API extras, local Windows and Linux paths. No test uses live providers; outbound networking is blocked.
- **§9 build order:** milestone commits are ordered through T15. The real offline media demo, CLI, deployment kit and model research are implemented; T16 remains in progress.

## External verification and deliberate limits

1. API credentials, enabled requested model names, accurate relay prices and real provider access must be supplied for manual smoke checks. Unknown configured prices warn and record zero; estimates are not provider invoices.
2. Real ComfyUI workflows, custom-node/model versions and GPU memory requirements need deployment exports and live validation. Placeholder templates are never represented as runnable models.
3. A verified local FFmpeg/ffprobe build is available under ignored data/tools/ffmpeg; check.ps1 enables it, and the real-media tests and offline MP4 demo pass. Two Windows symlink tests skip where the OS cannot create symlinks. No binaries or generated media are committed.
4. Starlette's installed TestClient emits an upstream httpx deprecation warning; all API tests pass. This warning does not weaken network isolation or quality checks.
5. Shot-count changes intentionally require a new project. Existing shot fields, asset replacements and version reverts are incremental. This is the documented minimal graph-edit policy for the current tasks.
6. This is a local single-user application bound to loopback. Multi-user authentication/authorization is outside the specified milestones.
7. A human must inspect final visual/voice quality and rights; offline fakes prove control flow, not model fidelity or legal rights.

## T15 deployment research audit

The model research separates code/weight/auxiliary licenses, published memory evidence and unmeasured estimates. Opt-in model profiles pin revisions and verify published weight digests. Model installation, GPU inference, custom-node compatibility and real exported workflows still require the operator's environment; the manifest alone is not a complete model deployment.

## Remaining local robustness work (T16)

Concurrent LLM calls can read stale project spend and exceed the project limit in aggregate. A FakeProvider reproduction requires no external service. T16 must serialize/reserve these calls before claiming the final quality audit is complete.
