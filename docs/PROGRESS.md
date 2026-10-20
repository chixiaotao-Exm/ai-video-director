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
## T04 — ComfyUI client and workflow templates

- Added a synchronous HTTP client with upload, submit, poll, output download, timeout, and node-error handling.
- Added strict typed workflow template rendering and placeholder contracts for image-to-video, TTS, and lipsync.
- Added configurable local/remote ComfyUI profiles and offline MockTransport tests.
- Added test network blocking and manual smoke script.
- Tests: 49 passed.

## T05 — LLM routing, cost ledger, and budget guard

- Added role-based routing, normalized tool calls/usage, one structured repair retry, three lazy SDK adapters, and FakeProvider.
- Added CostEntry/CostLedger, actual usage accounting, budget/confirmation checks, and relay configuration.
- Added SDK-double contracts and Gemini inline/file-upload tests without network; manual LLM smoke skips without keys.
- Tests: 73 passed.
- External requirements: enabled model names, provider/relay keys and accurate price table. Gemini has no guessed default. Real SDK calls were not exercised.

## T06 — analysis pipeline

- Added ffprobe/FFmpeg steps, optional scene detection and CPU Whisper transcription, validated structure/style-only schemas, and Gemini video understanding.
- Added reusable analysis orchestration with injected fakes, separate output directories, immutable versions, cost recording, and failure-safe publication.
- Tests: 102 passed, 1 skipped (FFmpeg/ffprobe not available on PATH). Manual analysis smoke exits normally without --video.
- External requirements: FFmpeg, optional analysis dependencies/model downloads, Gemini configuration. No real service called.

## T07 — script and storyboard

- Added validated storyboards generated through the script role with consent checks and originality instructions.
- Applied per-shot character/voice/camera recipes, connected analysis to the shared script, and preserved immutable snapshots.
- Added targeted edits and same-count reapplication that dirty only changed chains.
- Tests: 132 passed, 1 skipped (FFmpeg unavailable). No live model used.
- Deliberate limit: changing shot count requires a new project; field edits are incremental.

## T08 — executors and incremental rebuild engine

- Added reference-image generation, ComfyUI video/TTS/lipsync, duration-based SRT burn-in, final concatenation/audio normalization and visible/metadata AI labels.
- Added transactional scheduling, pending budget checks, dependency-aware caching, callback/restart reconciliation, consent gates and stale-completion preservation.
- Added two-shot end-to-end tests with real queue/fake handlers, incremental edits, revert cache, failed prerequisites, revoked consent, duplicate scheduling and callback replay.
- Tests: 195 passed, 2 skipped (real FFmpeg media checks unavailable on this PATH).
- External requirements: exported runnable ComfyUI workflows/models, enabled image API key/model, explicit executor cost estimates, FFmpeg with libx264/subtitles/drawtext filters. No paid calls made.

## T09 — director and HTTP API

- Added all 17 validated director tools, persisted tool-use history, bounded loops, live SDK routing through the router, and durable human reviews/budget approvals.
- Added project/source/asset/consent/chat/status/version/revert/job APIs, safe bounded uploads/media serving, and SSE from durable jobs.
- Added shared Runtime and API-lane analysis/script handlers; executor start rechecks stale recipes, reviews, budget and consent. Asset replacement updates materialized recipes and dirties only relevant chains.
- Tests: 245 passed, 3 skipped (two FFmpeg cases and Windows symlink capability), one upstream Starlette/httpx deprecation warning.
- Deployment is a local single-user app; provider keys, workers and exported ComfyUI workflows are still required for real generation. No paid calls made.

## T10 — minimal web workspace

- Added a responsive three-column Chinese workspace: projects, source/asset uploads, explicit consent, assignment, chat, structure/script review, shot media/version selection/revert, final preview, budget and live jobs.
- Packaged static files; documented full setup/workflow and supplied a small worker runner alongside uvicorn.
- Verified local page layout in the in-app browser, static/API contracts, and JavaScript syntax.
- Tests: 248 passed, 3 skipped; one upstream Starlette/httpx deprecation warning remains for T11 review.
