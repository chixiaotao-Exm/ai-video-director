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
