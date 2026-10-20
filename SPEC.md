# AI Video Director — System Spec

A stateful "AI video director" agent. The user gives it a sample video; it analyzes the
structure, then creates a *similar-style* (not copied) video starring the user's own saved
character and cloned voice. The user refines the result through conversation. Edits must be
**incremental**: only the parts affected by a change are regenerated.

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
- Sub-agents never talk to each other. They read project state, write results, and return a
  short summary to the director.
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
- Never clone the person from the sample video; the sample only provides structure/style.
- Final render adds an "AI-generated" label (burned-in text + metadata).

## 8. Tech stack

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + SQLite, httpx, pytest. FFmpeg on PATH for
media steps. Claude Agent SDK / Anthropic SDK for the director, OpenAI SDK for GPT + GPT Image,
google-genai for Gemini. Runs on Windows (dev laptop, RTX 5070 laptop) and Linux (rented GPU).

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
