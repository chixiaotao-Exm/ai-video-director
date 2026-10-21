# AI Video Director — System Spec

A stateful "AI video director" agent. In `remake` mode, the user gives it a sample video; it analyzes the
structure, then creates a *similar-style* (not copied) video starring the user's own saved
character and cloned voice. The user refines the result through conversation. Edits must be
**incremental**: only the parts affected by a change are regenerated.
`anime_sequel` continues a story. The independent `performance_remix` mode preserves a
reference performance's choreography, song and timeline while replacing the target
character, singing voice and scene, as specified in section 10.

## 1. Architecture

```
User
 ↓ chat
Director Agent (Claude)        ← the only agent that talks to the user and makes decisions
 ↓ tool calls
 ├─ LLM sub-agents (reasoning)          ├─ Executors (deterministic, plain functions)
 │   ├─ Video analysis  (Gemini)        │   ├─ Image gen: character sheet, first frames (GPT Image API)
 │   ├─ Script / storyboard (GPT)       │   ├─ Image→video (ComfyUI: local GPU or rented Linux GPU)
 │   └─ QA / frame checker (GPT)        │   ├─ Voice clone / TTS (ComfyUI or local service)
 │                                      │   ├─ Lip sync (ComfyUI)
 │                                      │   ├─ ASR (Whisper), scene detection (PySceneDetect)
 │                                      │   └─ Edit / subtitles / mux (FFmpeg)
 ↓ all read/write
Project state (SQLite + file storage)  ← single source of truth
```

Rules:
- The director and a worker sub-agent may converse. The worker asks with `message_director`
  and waits; the director replies with `message_subagent`. Workers do not talk to each other
  or to the user. Analysis, script, and QA calls stay one-shot: they read project state, write
  results, and return a short summary to the director.
- Anything deterministic is a plain function, not an agent.
- All model calls go through one routing layer (`llm/`), so providers/models change via config.
- Every paid call is recorded with an estimated cost; the director enforces a per-project budget
  and asks the user before large batch generations.

## 2. Project state (the core)

### 2.1 Entities

- **Asset library (global, shared across projects)**
  - `Character`: id, name, reference images (front/side/half/full), fixed appearance prompt,
    optional LoRA path, `consent_confirmed` (bool, required before use), created_at.
  - `Voice`: id, name, sample audio paths, engine + engine voice id/model path, default
    speed/emotion params, `consent_confirmed` (bool, required before use).
- **Project**: id, title, source video path, analysis result (JSON), character_id, voice_id,
  budget_limit, spent, created_at.
- **Node** (a unit of generated content with a recipe), kinds:
  - `analysis`, `script`, `shot_frame` (first frame image), `shot_video`, `voiceover`,
    `lipsync`, `subtitles`, `final`
  - Each node has: id, project_id, kind, shot_index (nullable), `recipe` (JSON: prompts, ref
    images, seed, model + version, params…), `status` (`clean` | `dirty` | `running` | `failed`),
    `current_version_id`, `depends_on` (list of node ids).
- **Version** (immutable): id, node_id, number (1,2,3…), recipe snapshot, output paths,
  `recipe_hash`, cost, created_at, note (e.g. "user: make it brighter").

### 2.2 Dependency graph per shot

```
script ─┬→ shot_frame → shot_video ─┐
        └→ voiceover ───────────────┴→ lipsync → subtitles → final
```
`final` depends on every shot's `subtitles` (or `lipsync`) node.

### 2.3 Incremental regeneration (make-style)

- Changing a node's recipe marks it `dirty` and propagates `dirty` to all transitive dependents.
- `plan_rebuild(project)` returns the dirty nodes in topological order.
- A node whose new `recipe_hash` equals an existing version's hash reuses that version (cache hit),
  no regeneration.
- `revert(node, version_number)` sets `current_version_id` back and marks dependents dirty.
- Example: "change shot 2's line" → `voiceover[2]` dirty → `lipsync[2]` → `subtitles[2]` →
  `final`. Shots 1 and 3 untouched.

## 3. Job queue

- GPU jobs (video, TTS, lipsync) run **serially** through one queue (VRAM fits one big model).
- Queue groups jobs by model type to reduce model load/unload.
- Submit is non-blocking: the director gets a job id, keeps chatting, and is notified on finish.
- Jobs persist in SQLite so a restart doesn't lose them. Failed jobs are retryable.

## 4. ComfyUI backend

- Each capability = a workflow JSON template in `workflows/` with placeholders.
- Client: submit workflow to `/prompt`, poll `/history/{id}`, download outputs.
- Backend URL comes from config: local laptop GPU (drafts) vs rented Linux GPU (final). Switching
  is a config change only.

## 5. Director tools (exposed to Claude)

`analyze_video`, `create_character`, `create_voice`, `write_script`, `update_shot`,
`generate_frames`, `generate_videos`, `generate_voiceover`, `run_lipsync`, `build_subtitles`,
`render_final`, `plan_rebuild`, `rebuild`, `revert`, `list_versions`, `get_project_status`,
`get_budget`.

Each tool validates input with Pydantic and returns a short text summary.

## 6. User workflow & checkpoints

1. Upload sample video → director runs analysis → **user reviews the structure template**.
2. First time only: create character (image API, user approves the sheet) and voice (user uploads
   1–3 min clean sample, confirms consent).
3. Director writes script/storyboard → **checkpoint 1: user approves script**.
4. First frames generated → **checkpoint 2 (most important): user approves frames** (cheap to fix).
5. Video, voice, lip sync, subtitles, final render → **checkpoint 3: user reviews the video**.
6. User asks for changes in chat → incremental rebuild → new versions.

## 7. Compliance

- Face/voice cloning requires `consent_confirmed=True` on the asset; tools refuse otherwise.
- In `remake`, the sample provides structure/style. In `performance_remix`, it also supplies
  choreography, vocals and accompaniment; target identity and voice are independently
  selected, consented library assets. Do not infer or enroll a target identity from the source.
- Final render adds an "AI-generated" label (burned-in text + metadata).

## 8. Tech stack

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + SQLite, httpx, pytest. FFmpeg on PATH for
media steps. Claude Agent SDK / Anthropic SDK for the director, OpenAI SDK for GPT, GPT Image,
and xAI Grok chat plus Grok Imagine stills, google-genai for native Gemini. Script, QA, analysis,
web search, and still images can each select a configured provider. Runs on Windows (dev laptop,
RTX 5070 laptop) and Linux (rented GPU).

## 9. Build order (one Codex task each)

1. Project state models + versioning + dirty propagation + tests  ← start here
2. Asset library (characters, voices, consent gate)
3. Job queue (persistent, serial GPU lane, retries)
4. ComfyUI client + workflow templating (with a fake server for tests)
5. LLM routing layer + cost ledger + budget guard
6. Analysis pipeline (scene detect, Whisper, Gemini)
7. Script/storyboard sub-agent (structured output)
8. Executors: image gen, TTS, lipsync, FFmpeg assembly/subtitles
9. Director agent + tools + chat API
10. Minimal web UI for chat + checkpoints


## 10. Performance Remix (`performance_remix`)

### 10.1 Scope and inputs

This mode retains the source choreography, lyrics, melody, accompaniment and performance
timing. It replaces the visible performer, singing timbre and scene. Singing voice conversion
is audio-to-audio; ordinary script/TTS generation is not a substitute. The first release
supports one performer and a complete video preview over an explicitly selected interval
of at most ten seconds. Scene images and singing references are optional checks before
that video, not a substitute for it. Continuing generation requires review of the actual
preview MP4; it does not automatically extend the saved interval. Long-video execution is
not yet available. Multi-person performances, new lyrics/composition and arbitrary camera
changes are outside this first release.

Inputs are a source video and explicit interval, a consented library character (face and,
when available, full-body images), a consented voice with a usable singing model version or
samples for preparing one, and a scene prompt with optional reference image. Samples alone
do not imply a ready RVC model. Preserve input files and store media paths relative to
`data_dir`; record content hashes and selected asset/model/workflow versions.

### 10.2 Persistent graph and timeline

The independent graph is:

```
performance_prepare ─┬→ vocal_separation → singing_voice → performance_mix ──────┐
                    │                                                        │
                    └→ motion_video ← scene_reference                       │
                           │                                                 │
                           └→ performance_sync ← singing_voice               │
                                      │                                      │
                                      └→ optional performance_lipsync         │
                                                   └─────────────────────────┴→ performance_final
```

`performance_final` consumes `performance_sync` when lip correction is disabled. Scene
reference consumes the selected character and scene configuration. Motion generation
consumes the prepared source motion video; pose preprocessing is required only when the
selected real workflow needs it. The old script/voiceover graph is not used in this mode.

Preparation records source SHA-256, selected interval, video/audio stream starts, timebases,
frame rate, rotation, audio sampling, and source-to-prepared timing. Keep frame timestamps
or a reversible mapping when normalizing variable frame rate. Reject missing video/audio
with an actionable error. Preserve audio offsets, pad missing audio with silence, and
measure actual output frames/duration. Never stretch a native model output silently to fit
the requested duration. Persist model limits and trim only according to explicit mapping.

New kinds/configuration require an idempotent SQLite upgrade with old project/version
compatibility tests. Queue, cost/consent guards, review gates, caching, cancellation, restart
and retry apply to this graph as they do to existing jobs. An unavailable model/workflow
must fail readiness explicitly; a fake backend cannot be presented as a generated result.

### 10.3 Incremental regeneration

- Scene/character changes invalidate scene reference, affected motion video, sync, enabled
  lip correction and final; confirmed singing vocals and accompaniment remain reusable.
- Voice/model changes invalidate conversion, mix, sync, enabled lip correction and final;
  prepared motion and base generated video remain reusable.
- Mix gain changes invalidate mix and final while retaining conversion and video.
- Source interval/time mapping changes invalidate preparation and all true descendants.
- A segment conditioned on the previous segment's tail is a real downstream dependency.

Every recipe fingerprint includes effective parameters, input hashes, actual model and
workflow versions and dependency versions. Versions and their media remain immutable.

### 10.4 Backends and acceptance

Initial candidates are RVC for singing timbre conversion, Wan Animate through ComfyUI for
motion/identity/scene, and existing FFmpeg/Demucs utilities for preparation, stems and mux.
These are candidates until S01 produces real output. Local and remote GPU routes share
the serial GPU lane. A model import/training process records version, hashes, index/config
and readiness; a selected voice must not silently fall back to TTS or the original singer.

Acceptance has four distinct layers: offline fake-backed tests, real local FFmpeg media
tests, real model samples, and desktop/mobile browser flows. The real sample bundle must
contain the source, converted singing preview, generated character/scene video and final
MP4, plus GPU/peak memory, elapsed time, model/node/workflow versions, parameters, output
resolution/frame count/duration, input hashes and failures. Compare start/middle/end action
and beat anchors, identity, scene, feet/hands/occlusion, singing high/long notes and lip sync.
Human review confirms timbre and visual quality. Record unresolved quality limits; tests
with fake model outputs cannot satisfy this gate.

The director and HTTP API share the same performance configuration, readiness,
prepare/preview/video-preview/generate and read-only segment-plan service. Director tools are scoped to `performance_remix`;
script/TTS authoring is unavailable in this mode. Partial configuration preserves omitted
inputs and frozen workflows, and active node jobs prevent configuration changes. A scene
edit invalidates only its actual visual descendants, retaining valid singing versions.
Source preparation is deterministic. `POST /projects/{id}/performance/preview` requires
explicit human `performance_inputs` review and queues only preparation, separation,
scene reference, singing conversion and mix; it produces no motion video or final MP4.
`POST /projects/{id}/performance/video-preview` uses the same input approval, requires ready
motion and enabled lip workflows, and queues the complete video graph over the saved
interval of at most ten seconds. A longer interval is rejected rather than silently cut.

`performance_preview` requires the current clean scene, singing, motion, sync, mix and final
versions, plus the lip version when enabled and a relative final video output path. It can
be approved only after the user watches that actual video. References or an older approval
of image/audio outputs cannot satisfy this gate. `POST /projects/{id}/performance/generate`
requires that approval and operates on the current configured interval; it may reuse valid
outputs and does not enable arbitrary-length execution. `performance_final` separately
records review of the resulting MP4. Both output approvals bind the exact configuration and
all reviewed video/audio versions. Conversation approvals quote the latest actual user's approving words;
questions, negations and background notifications cannot approve checkpoints. Asset consent,
path validation, budget reservations and queue guards apply equally to both entry points.

Long-video release additionally requires segmentation, continuity, queued/running/failed
status accuracy, cancellation/restart, retry and historical-version rollback acceptance.
Stages and evidence are tracked in `tasks/T18-performance-remix/`.

### 10.5 Read-only long-interval planning

`POST /projects/{id}/performance/segment-plan` accepts `{duration_s, context_frames=0}`;
the director uses `plan_performance_segments` for the same operation. Planning requires a
current clean preparation version, a schema-valid preparation manifest matching the saved
request and current source hash, and the frozen motion workflow. It plans a proposed total
duration from the saved source start, within the measured source video stream duration,
using explicit native frame and audio sample coordinates. It does not infer source length
from a container or the prepared preview, retime media, modify configuration, approvals or
versions, or enqueue jobs.

The result records segment boundaries, context, declared tail padding and exact fixed-frame
contract mismatches. Invalid request fields return 422; missing/stale evidence or an
unplannable interval returns 409. A successful 200 response always reports
`execution_available=false`, `model_generated=false` and `quality_verified=false`, even
when every proposed segment meets the fixed-length contract. Segmented execution,
cross-segment identity/motion/audio continuity, restart/retry and a real long sample remain
required before S07 can be accepted.
