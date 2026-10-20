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

## T13

- **Date:** 2026-10-20
- **Decision:** Use argparse and the existing Runtime/DirectorTools; CLI outputs JSON for automation and concise errors with nonzero exits. Analysis imports explicit local video inputs, while asset creation accepts uploaded-relative paths consistently with the web tool boundary.
- **Alternatives:** A new CLI framework or separate business logic; rejected to keep command behavior aligned with HTTP/director gates.
- **Decision:** CLI generation retains human approvals from the web workflow. Worker commands run one selected lane and support explicit recovery; no model is called by project/asset/status/version commands.
- **Alternatives:** Silently auto-approve in CLI; rejected because user checkpoints must remain enforced.

## T14

- **Date:** 2026-10-20
- **Decision:** GPU setup defaults to CUDA 13.0 PyTorch wheels, which meet the requested CUDA12.8+ baseline and current ComfyUI instructions. The index and checkout revision remain configurable; a real CUDA tensor operation verifies the selected host/driver.
- **Alternatives:** Force an obsolete CUDA12.8 index against current ComfyUI; rejected after official documentation verification.
- **Decision:** ComfyUI and the app bind loopback. Compose uses host networking for a host SSH tunnel and starts separate GPU/API/CPU workers sharing one database volume. Models are an explicit manifest choice, never guessed or downloaded by tests.
- **Alternatives:** Public ComfyUI ports or container loopback on an isolated bridge; rejected for exposure and connection correctness.

## T15

- **Date:** 2026-10-20
- **Decision:** Use Wan2.2 TI2V-5B, CosyVoice3 and MuseTalk1.5 as the permissive evaluation baseline in all three memory tiers. Larger tiers initially provide headroom; optional larger models remain documented evaluations, not unmeasured compatibility promises.
- **Alternatives:** Select the largest advertised model per tier; rejected because official memory requirements, weight licenses and wrapper compatibility differ.
- **Decision:** Model installation is opt-in through named profiles; the default downloads nothing. Pin Hugging Face revisions and verify published LFS SHA-256 hashes. Auxiliary models and actual ComfyUI workflow exports still require operator validation.
- **Alternatives:** Automatic multi-gigabyte downloads or floating model URLs; rejected for reproducibility and explicit deployment choice.
- **Decision:** Keep code licenses, weight licenses, auxiliary dependency licenses, publisher minima and planning estimates separate in the research document. Record the 2026-10-20 access date and primary sources.
- **Alternatives:** Treat an open repository as blanket commercial permission or a VRAM guarantee; rejected because neither follows from repository visibility.

## T16

- **Date:** 2026-10-20
- **Decision:** Configure plain/JSON stderr logging at application entry points; attach request/worker context without logging HTTP bodies. Health checks only touch the local database, and JSON metrics are read-only persisted queue/spend summaries.
- **Alternatives:** Probe paid providers from health checks or log full request payloads; rejected because liveness checks must be cheap and should not expose user content.
- **Decision:** Production workers spawn an execution process from serializable Settings and rebuild Runtime there. The supervisor retains its heartbeat/claim until the entire local process tree stops, then completes/fails the attempt. Windows Job Objects and POSIX process groups own descendants; graceful shutdown drains within a configurable grace period.
- **Alternatives:** Time out a thread and release its lane; rejected because the original handler could keep executing alongside its retry. Fake/demo workers remain inline unless an isolated runner is supplied.
- **Decision:** Unconfirmed process cleanup quarantines a running claim with a non-expiring lease and stops that supervisor. Recovery is an explicit operator action after stopping old processes; ordinary polling never releases quarantined GPU work.
- **Alternatives:** Automatically expire an uncertain live process; rejected because that can violate GPU serialization. Accepted remote requests may still run or bill after local cancellation, so exactly-once remote execution is not claimed.
- **Decision:** All budget decisions and accounting share a nonblocking per-database/project OS lock until the outer transaction commits or rolls back. Concurrent conflicts fail visibly instead of waiting while holding SQLite write locks. Pending unledgered executor attempts stay reserved through completion/retry gaps.
- **Alternatives:** Refresh spend without mutual exclusion, or hold SQLite write locks across network calls; rejected because the first races and the second blocks queue heartbeats. A separate distributed reservation service is beyond the single-host SQLite deployment.

## T17a — settings and browser API

- **Date:** 2026-10-20
- **Decision:** Follow T17's explicit browser-settings requirement: store editable settings, including replaced keys, in local gitignored data/settings.json. This supersedes the older env-only convention for this task. Precedence is environment/.env < settings file < explicit constructor/CLI overrides; a dictionary field replaces the lower-priority dictionary completely, including an empty object. Atomic replacement preserves the previous file on validation/write failure. Data/database paths are read-only from the browser.
- **Alternatives:** Rewrite .env or return stored keys to form fields; rejected because local application settings and write-only credentials are explicitly requested. Responses expose only a masked suffix; validation/diagnostic errors omit secret inputs.
- **Decision:** All HTTP routes enforce loopback Host and same-origin mutations. IPv6 ::1 is treated as loopback; absent Origin remains valid for local CLI clients, while foreign/null Origin and explicit cross-site requests are rejected.
- **Alternatives:** Protect settings alone or disable checks for a test hostname; rejected because uploads/media and other state-changing operations need the same boundary. Tests use a real loopback base URL.
- **Decision:** Connection checks occur only after an explicit button/API action. Paid provider checks use a tiny eight-token request and require a selected project so normal budget and ledger rules apply. Dashboard shows the last check or unverified, never background paid probes.
- **Alternatives:** Untracked free-standing test calls or automatic service probing; rejected because model calls must be budgeted and user-triggered.
- **Decision:** Shot regeneration changes only the requested node's generation nonce and scopes scheduling to its dependent chain. Human reviews and budget approvals are rechecked; any newly required review is returned to the UI. Existing versions remain immutable and unrelated dirty shots are not queued.
- **Alternatives:** Rebuild the entire project or auto-approve after regeneration; rejected for incremental behavior and review integrity.
- **Decision:** Restrict pytest discovery to tests/ so ignored generated data and draft artifacts cannot become test modules.
- **Alternatives:** Rely on filename conventions under data/; rejected because data is not source-controlled test input.

## T17b — managed browser workers

- **Date:** 2026-10-20
- **Decision:** avd serve defaults to one managed process for each GPU/API/CPU lane; --no-workers disables automatic startup. Browser controls only the server-owned processes, while status also shows independently running queue work.
- **Alternatives:** Require three extra terminals or kill every worker sharing the database; rejected for the one-command browser workflow and ownership boundaries.
- **Decision:** Reload effective settings between jobs and reuse Runtime until configuration changes. Preserve constructor overrides, model grouping, queued execution snapshots and immutable outputs.
- **Alternatives:** Change an in-flight handler's configuration or require app restart after every saved setting; rejected for predictable execution and browser usability.
- **Decision:** On forced/abnormal managed process exit, fence only its exact unfinished claim and retain a permanent quarantine lease. Rotate its claim token so a late heartbeat/result cannot release the lane. Shutdown signals all children before bounded waits and leaves external workers untouched.
- **Alternatives:** Recover every running job on server shutdown; rejected because another worker may still own valid work.
