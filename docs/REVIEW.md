# SPEC review — 2026-10-20

This review covers SPEC.md sections 1–9 and the T03b–T16 plus T17 milestone requirements. PROGRESS.md preserves the validation result for each milestone; the final local quality gate covers the complete implementation.

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
- **§9 build order:** milestone commits are ordered through T16. The offline real-media demo, CLI, deployment kit, model research, observability and worker robustness are implemented.

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

## T16 follow-up audit

- Concurrent LLM calls previously checked stale spend. BudgetGuard/router now acquire a nonblocking per-database/project OS lock until the outer transaction commits/rolls back. Scheduling and accounting use the same lock; queued work and unledgered executor attempts remain reserved. FakeProvider, cross-process, rollback, nested-transaction, completed-sheet and callback-replay tests cover the reproduced races. Conflicts are visible as BudgetBusy/HTTP 409 and use ordinary finite worker retries.
- Application entry points configure text/JSON logging; HTTP and worker events carry project/job context. Health returns 503 on a database failure, metrics report persisted queues/running jobs/spend, and Docker probes /health.
- Production workers reconstruct Runtime in spawned processes, enforce default/per-type timeouts, drain on SIGINT/SIGTERM up to the configured grace, stop the local process tree before releasing the claim, and retain a non-expiring quarantine lease if cleanup cannot be confirmed. Tests use actual Windows child/grandchild processes, verify heartbeat/serialization and exhausted retries, and test POSIX group/watcher logic without network calls.
- Windows Job Objects also stop descendants on supervisor death. POSIX children monitor the original parent PID and terminate their own process group if orphaned. Actual Linux process integration still needs the target host; a native extension continuously holding the GIL can delay the in-process watcher. SIGKILL/system failure is not presented as graceful shutdown.
- External calls remain at-least-once: remote ComfyUI/provider work already accepted can continue or bill after local timeout. A timeout before provider usage is returned/committed requires invoice reconciliation. An authoring process killed after its version commit but before queue completion can create another immutable version on retry. Exactly-once external execution/transactions were not promised by these milestones.

## Milestone requirement checklist

| Milestone | Audited evidence |
| --- | --- |
| T03b | Attempt exhaustion on stale recovery; callback exceptions logged without stopping the worker loop. |
| T04 | Upload/submit/history/download, typed placeholder validation, local/remote profiles, offline MockTransport errors/timeouts; real workflow exports remain explicit deployment inputs. |
| T05 | Provider interfaces/fakes, role defaults and overrides, structured repair, usage ledger, budget/confirmation checks; live SDK imports are lazy. |
| T06 | Probe/scenes/audio/ASR/structure-only understanding, immutable analysis publication, optional heavy dependencies and actual FFmpeg clip test. |
| T07 | Structured storyboards, shared graph application, field-to-node incremental updates and unaffected-shot tests. |
| T08 | Image/Comfy/media handlers, relative immutable attempt outputs, subtitles/AI label, incremental rebuild/cache/revert/failure/consent/budget tests. |
| T09 | All 17 tools, persisted tool-loop chat, separate human approvals, nonblocking jobs, safe uploads/media, status/versions/revert/SSE tests. |
| T10 | Served static workspace, asset consent, shot previews/reverts, numbered checkpoints, SSE/status/budget display and documented setup/workflow. |
| T11 | Ruff/format/mypy configuration and check scripts; state/queue/engine strict typing; identified local integrity and budget races fixed with regressions. |
| T12 | Real offline three-shot MP4 via production queue/engine; visible subtitles/disclosure; shot-two-only chain regeneration and cache reuse. |
| T13 | Installed argparse CLI for project/assets/consent/analysis/chat/rebuild/versions/revert/jobs/worker/serve; non-LLM tests. |
| T14 | Ubuntu GPU checks/venv/custom nodes/model downloader, Blackwell-capable torch selection, loopback service/tunnel guide, app Docker/Compose; Bash syntax/dry-run tests. |
| T15 | Current candidate tables, source/access dates, license/memory/node/Blackwell distinctions, three opt-in baseline tiers, pinned manifest/hash verification and workflow guide. |
| T16 | Contextual structured logs, database health/JSON metrics, spawned deadlines, bounded shutdown, retry exhaustion, process-tree cleanup/quarantine and concurrency regression tests. |

All implementation checks are offline. Real API access, rented GPU installation, Docker image execution, actual exported ComfyUI graphs and model fidelity remain external validation, not passing offline-test claims.

## T17a browser API audit

Added regression coverage for settings precedence/atomic failure/secret masking (including malformed inputs), loopback Host and cross-origin denial, all workspace read/actions, budget floors and reservations, retry consent/review/stale-recipe checks, scoped regeneration and safe thumbnails/log redaction. Connection probes are explicit user actions only; model probes use the normal project ledger. T17's requested local settings file intentionally supersedes the older env-only storage convention. Browser UI and managed worker integration remain the following T17 commits.

## T17b lifecycle audit

Managed worker start is idempotent, lane controls preserve other workers, stop is bounded and rotates unfinished claim tokens on uncertain cleanup. API tests verify lifespan startup/shutdown and origin enforcement; CLI tests verify default/explicit flags. Worker status distinguishes server-owned process state from all running queue jobs. Browser UI integration and visual checks remain T17c/T17d.

## T17 completion audit

- All requested browser surfaces are implemented: dashboard/service snapshots; project overview/chat/storyboard/shot media/final/analysis; characters/voices/consent; global queue controls; ledger/budgets; masked provider/Comfy/general/advanced settings; worker/health/metrics/version/FFmpeg/log controls.
- Local security applies to settings, static, media and state-changing routes. Connection checks require user initiation; paid tiny checks use project budget/ledger. Configuration precedence and atomic persistence have offline API tests.
- Default avd serve owns three lane workers with bounded shutdown and browser controls; externally launched workers are observed but never killed. New settings reload between jobs.
- Node syntax checks cover all modules in both quality scripts. The built wheel was inspected and includes the nested page and i18n assets.
- Headless Edge verified 38 desktop/mobile page/region captures and two consent-dialog captures. All 40 PNGs were visually inspected; overflow, narrow-table wrapping, player sizing, mobile budget/header layout, duplicate historical SSE notifications and edit-preservation issues were fixed. Ten real browser form/control flows passed against a fresh local offline fixture, with no model calls.
- Screenshots contain synthetic content and unconfigured provider panels. Existing user-entered demonstration settings and the service using them were preserved; temporary validation servers/workers/browsers were cleaned up.
- External boundaries remain: no real paid provider, GPU inference, rented-host installation or real exported ComfyUI graph was exercised. Offline controls/visual checks do not certify model quality or hardware compatibility. This remains a local single-user application.
