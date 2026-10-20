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
## T04

- **Date:** 2026-10-20
- **Decision:** ComfyUI integration uses synchronous `httpx.Client` with injectable transports; production calls remain explicit while tests use `MockTransport`.
- **Alternatives:** Async client; rejected because the current queue workers and executor interfaces are synchronous.
- **Decision:** Checked-in workflows are strict placeholder contracts and must be replaced with exported ComfyUI API JSON before real generation.
- **Alternatives:** Pretend placeholder graphs are runnable; rejected because model/node availability is deployment-specific.

## T05

- **Date:** 2026-10-20
- **Decision:** Preserve the specification's model defaults as configuration, without claiming that any provider account or relay exposes them. Gemini requires an explicit model.
- **Alternatives:** Guessing alternative model names; rejected because the user supplied exact defaults.
- **Decision:** `budget_limit=None` means unlimited; `0` means a literal zero budget. A check does not charge; completed calls are charged through the cost ledger in the caller's transaction.
- **Alternatives:** Treat zero as unlimited; rejected because it silently disables the budget guard.
- **Decision:** Prices are configured in USD per million tokens. Unknown rates warn and record zero as required; deployment must fill its provider/relay price table.
- **Alternatives:** Hard-coded unverified prices; rejected because relay prices and model access vary.
- **Decision:** The router estimates text input size plus configured maximum output tokens before a call, then records provider-reported usage afterward. Estimates are advisory; callers may supply an explicit estimate for multimodal calls.
- **Alternatives:** Claim exact prices/token counts before execution; rejected because provider usage is only known afterward.

## T06

- **Date:** 2026-10-20
- **Decision:** Analysis input must resolve within data_dir; the API/CLI will import external sources there. Each analysis run uses a new output directory so old version files remain untouched.
- **Alternatives:** Persist arbitrary absolute source paths; rejected because project storage must be portable.
- **Decision:** Silent video has an empty transcript. Real shot detection and Whisper remain optional, lazy imports; Whisper uses CPU/int8 by default to avoid competing with the serial GPU lane.
- **Alternatives:** Reject silent videos or automatically occupy the GPU; rejected as unnecessary restrictions.

## T07

- **Date:** 2026-10-20
- **Decision:** Storyboards use contiguous 1-based shot indexes and finite positive durations. Reapplying a same-sized storyboard reuses nodes, updates only changed recipes, and preserves every old version.
- **Alternatives:** Delete/recreate graphs or regenerate every shot; rejected because versions must be immutable and edits incremental.
- **Decision:** Changing shot count requires a new project for now. Targeted edits append a readable script snapshot without propagating from the shared script; only the actual frame/video/voice recipe edits propagate.
- **Alternatives:** Implement graph resizing/archive semantics; deferred because T07 specifies field updates, not shot insertion/deletion.

## T08

- **Date:** 2026-10-20
- **Decision:** Rebuild owns short DB transactions; authoring code commits before scheduling. Queue insertion and node RUNNING state are written atomically. Completion reconciliation is repeatable after lost callbacks.
- **Alternatives:** Submit jobs in a separate transaction from node changes; rejected because a crash could leave running nodes without jobs.
- **Decision:** Generated-node recipes include upstream output fingerprints. Shared script content is already mapped to each shot, so whole-script version changes do not invalidate unrelated shots. Stale job results are preserved as history but do not clear later edits.
- **Alternatives:** Hash only local recipes; rejected because changed audio would incorrectly reuse old lipsync/subtitles.
- **Decision:** Each attempt writes a separate subdirectory under its version run directory; files are never overwritten. TTS uses the first validated voice sample because the template accepts one sample.
- **Alternatives:** Overwrite partial retry outputs or invent multi-sample workflow inputs; rejected.
- **Decision:** Final media is normalized to the first shot's even dimensions, 30 fps, stereo 48 kHz audio, with silence for missing audio, visible AI-generated text and metadata. Each shot subtitle uses actual voice duration.
- **Alternatives:** Require identical generated stream formats or omit disclosures; rejected.

## T09

- **Date:** 2026-10-20
- **Decision:** Runtime composition is shared by HTTP, director and future CLI; tools are project-scoped and validate Pydantic inputs. Authoring work is queued in the API lane, so chat returns job IDs immediately.
- **Alternatives:** Run analysis synchronously inside chat; rejected because it prevents continued conversation.
- **Decision:** Human checkpoint and bounded budget approvals live in separate API operations, not model-callable tools. Approval fingerprints become stale when reviewed versions/recipes change; executor start rechecks consent, review and recipe freshness.
- **Alternatives:** Let the director pass self-asserted approval booleans; rejected because it bypasses user checkpoints.
- **Decision:** SSE polls persisted terminal job state and supports event cursors, so API and worker may be separate processes. Media serving only permits known media extensions inside data_dir, never DB or secrets.
- **Alternatives:** Process-local notification queues or exposing the whole data directory; rejected for persistence and containment.
- **Decision:** Test network guard permits only socketpair construction for Windows asyncio wakeup pipes; outbound service connections remain blocked.
- **Alternatives:** Disable network blocking for API tests; rejected.

## T10

- **Date:** 2026-10-20
- **Decision:** Serve one vanilla HTML/CSS/JS workspace from FastAPI with packaged static assets. No CDN, frontend build step or browser-stored secrets.
- **Alternatives:** Separate frontend framework/build server; rejected for this minimal local UI.
- **Decision:** Use SSE completion events plus a four-second status refresh to show both completion and queued/running progress. Human review/consent/budget inputs use explicit forms and are never auto-checked.
- **Alternatives:** Only poll or automatically confirm; rejected for responsiveness and approval correctness.

## T11

- **Date:** 2026-10-20
- **Decision:** Enforce immutable versions in both ORM and SQLite triggers, and preserve audit records by rejecting destructive cascades. Apply only known additive nullable schema upgrades to existing SQLite databases.
- **Alternatives:** Trust callers or recreate databases; rejected because both can lose project history.
- **Decision:** GPU serialization is database-backed with renewable attempt leases and claim-token fencing. Explicit retries retain attempt numbering; ordinary startup/worker polling recovers expired leases, while force recovery is an operator action after stopping old workers.
- **Alternatives:** A process-local lock or indiscriminate stale recovery; rejected for multi-worker correctness.
- **Decision:** Record estimated external executor attempts even when downstream decoding/probing fails; unique job/attempt keys prevent duplicate charges. Actual provider billing still requires supplied price tables.
- **Alternatives:** Charge only successfully published versions; rejected because failed paid work still costs money.
- **Decision:** Current human budget approvals are revalidated for each call, including repair. Execution metadata is bookkeeping; approval content hashes exclude _inputs/_execution while staying bound to authored content and the bounded amount.
- **Alternatives:** Carry a permanent confirmed boolean through queue retries; rejected as an approval bypass.
- **Decision:** Add a prompt-only fictional sheet path with explicit human review/consent; do not use uploaded biometric references before authorization.
- **Alternatives:** Leave the sheet handler unreachable or auto-consent generated assets; rejected.

## T12

- **Date:** 2026-10-20
- **Decision:** The offline demo uses FFmpeg-only synthetic cards, tones and passthrough media, but still exercises the production ProjectRepo, queue, workers and RebuildEngine. It creates an isolated run database and only replaces a convenience export outside immutable version directories.
- **Alternatives:** Mock media or bypass the engine; rejected because the milestone requires a real MP4 and proves incremental behavior.
- **Decision:** Ambient paid cost/provider settings are ignored for this local stub; the demo project has a zero budget, zero estimated costs and no network-capable provider. The generated character/voice are synthetic and still pass through explicit consent fields.
- **Alternatives:** Require API keys or treat the synthetic assets as human identity; rejected for offline safety and compliance.
