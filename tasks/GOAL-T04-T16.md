# GOAL — Build milestones T03b → T16 of the AI Video Director

Repo: D:\code\ai-video-director. Work only inside it. Read AGENTS.md and SPEC.md first; they
win over anything ambiguous here. T01–T03 are done (state, assets, job queue) — build on them,
don't rewrite them.

## Ground rules for this goal (apply to every milestone)

1. Do milestones **in order**. Each milestone = its own commit, message `T0X: <title>`.
   Before each commit: `python -m pytest -q` with the project venv must be fully green.
2. **Tests never touch the network or real paid APIs.** Every external service (Anthropic,
   OpenAI, Gemini, ComfyUI, image API) sits behind an interface with a fake used in tests.
3. Real adapters are written too, but exercised only by manual smoke scripts
   `scripts/smoke_<name>.py`, which exit cleanly with a message when the needed env var/URL is
   missing.
4. Secrets only from env (`.env`, documented in `.env.example`, never committed).
5. Heavy/optional deps (faster-whisper, scenedetect, anthropic, openai, google-genai, fastapi,
   uvicorn) go in optional extras in pyproject (`[analysis]`, `[llm]`, `[api]`, `[all]`); core
   imports must not require them. Tests that need an optional dep use `pytest.importorskip`.
6. FFmpeg/ffprobe are called via subprocess with arg lists (never shell strings). Tests needing
   ffmpeg skip when it's not on PATH.
7. If you hit a decision that isn't covered, **don't stop**: pick the simplest reasonable option,
   record it in `docs/DECISIONS.md` (date, milestone, decision, alternatives), continue.
8. After each milestone append to `docs/PROGRESS.md`: what was built, test count, known gaps.
9. Don't add features beyond what's listed. Keep deterministic steps as plain functions.

## T03b — Queue review fixes (small, commit first)
- `recover_stale()` must respect attempts: a job left `running` whose `attempts >= max_attempts`
  becomes `failed` (error "worker crashed"), otherwise back to `queued`. Test both.
- An exception raised by `on_finished` is logged and must not stop `run_forever`. Test it.

## T04 — ComfyUI client + workflow templates
- `backends/comfyui.py`: `ComfyUIClient(base_url, timeout)` using httpx:
  `upload_file(path) -> name` (`/upload/image`), `submit(workflow: dict) -> prompt_id` (`/prompt`
  with a client_id), `wait(prompt_id, poll_interval, timeout)` (poll `/history/{id}`),
  `download_outputs(prompt_id, dest_dir) -> list[path]` (`/view`). Clear errors for ComfyUI
  node errors and timeouts.
- `backends/workflows.py`: load JSON templates from `workflows/`, substitute `{{name}}`
  placeholders (typed: strings, numbers, file refs), fail loudly on missing/unused placeholders.
- Ship placeholder templates `workflows/image_to_video.json`, `tts.json`, `lipsync.json` with a
  README explaining they must be exported from a real ComfyUI ("Save (API format)") and which
  placeholders each must expose.
- Config: `comfyui_profiles` (e.g. `local`, `remote`) + `comfyui_active_profile`, so laptop vs
  rented Linux GPU is a config switch.
- Tests with `httpx.MockTransport` simulating the full submit → history → view cycle and errors.

## T05 — LLM routing, cost ledger, budget guard
- `llm/router.py`: `LLMRouter` maps **roles** → (provider, model) from config:
  `director` → anthropic, `script`/`qa` → openai, `analysis` → gemini. Each provider supports a
  `base_url` override (relays). Methods: `chat(role, messages, tools=None)`,
  `structured(role, messages, schema: type[BaseModel]) -> BaseModel` (validated; one repair retry).
- Providers in `llm/providers/` (anthropic, openai, gemini) behind one Protocol; plus `FakeProvider`.
- Model names come from config with these defaults: director `claude-opus-5-5`, script/qa
  `gpt-6.1-sol`, image `gpt-image-2`, analysis: no default — must be set (`AVD_GEMINI_MODEL`).
- `llm/ledger.py`: `CostEntry` table (project_id, role, provider, model, input/output tokens,
  est_cost, created_at). Prices from a config table; unknown price → cost 0 + warning.
- `llm/budget.py`: `BudgetGuard.check(project, estimated_cost)` raises `BudgetExceeded` above the
  project limit and `ConfirmationRequired` when a single action exceeds `confirm_threshold`.
  Executors (T08) must call it before paid work; it updates `Project.spent` after.
- Tests with FakeProvider: routing, structured repair retry, ledger rows, budget errors.

## T06 — Analysis pipeline
- `agents/analysis/`:
  `probe(video) -> MediaInfo` (ffprobe), `detect_shots(video) -> list[ShotSpan]`
  (PySceneDetect, optional dep), `extract_audio(video) -> wav` (ffmpeg),
  `transcribe(wav) -> list[Segment]` (faster-whisper, optional dep, model size from config),
  `understand(video, shots, transcript) -> StructureTemplate` (Gemini via router role `analysis`).
- `StructureTemplate` (Pydantic): total duration, hook (first ~3 s) description, list of shot
  templates (duration, shot size, camera move, visual description, spoken-line purpose — not the
  verbatim line), pacing notes, music/voice style. It describes **structure and style only**.
- `analyze_video(project)` runs the steps, stores the template in the project's `analysis` node
  as a new Version (and in `Project.analysis_result`).
- Tests: each step with fakes; one ffmpeg-generated 3-second test clip when ffmpeg is present.

## T07 — Script / storyboard agent
- `agents/script.py`: `write_storyboard(template, character, voice, brief) -> Storyboard` via
  router role `script` + `structured()`. `Storyboard`: title, shots[ index, duration_s, line,
  visual_prompt, camera, emotion ].
- `apply_storyboard(project, storyboard)`: writes the script node recipe, creates the shot graph
  (T01 `create_shot_graph`) with per-shot recipes (frame: visual_prompt + character refs;
  voiceover: line + emotion + voice defaults; video: camera + duration).
- `update_shot(project, shot_index, changes)`: maps fields to the right node recipes via
  `update_recipe` (line/emotion → voiceover; visual_prompt → shot_frame; camera/duration →
  shot_video), so dirty propagation does the rest.
- Tests with FakeProvider; verify that changing a line dirties only that shot's audio chain.

## T08 — Executors + rebuild engine
- Job handlers registered per `job_type`:
  `image_gen` (OpenAI image API: character sheet + first frame from reference images),
  `image_to_video` / `tts` / `lipsync` (ComfyUI workflows via T04),
  `subtitles` (build SRT from script lines + voiceover durations; burn in with ffmpeg),
  `final` (concat shots, normalize audio, burn an "AI-generated" label, set metadata comment).
  Each handler stores outputs under `data_dir/projects/{id}/{node}/{version}/` (relative paths).
- `engine/rebuild.py`: `rebuild(project)` walks `plan_rebuild`; for each dirty node: cache hit
  via `find_cached_version` → reuse; else BudgetGuard check → submit job with the right
  lane/model_key; when a job finishes, `record_version` and continue with nodes whose
  prerequisites are now clean. Nodes of a failed job become `failed` and their dependents stay
  dirty. Consent gate: refuse to run frame/voice/lipsync nodes if the project's assets aren't
  usable.
- Tests: full 2-shot rebuild with fake handlers end-to-end; incremental rebuild after a line
  change only re-runs that shot's voiceover→lipsync→subtitles→final; cache reuse after revert;
  failure handling; budget refusal.

## T09 — Director agent + API
- `director/tools.py`: the tools from SPEC §5, each a Pydantic-validated function returning a
  short text summary; registered as tool schemas for the router role `director`.
- `director/agent.py`: tool-use loop (Anthropic messages API with tools) with a system prompt
  covering the three user checkpoints (script, first frames, final video), budget confirmation,
  and consent rules. Chat history persisted per project. Long jobs: tools submit and return job
  ids; the director reports progress from job status rather than blocking.
- `api/app.py` (FastAPI): create project + upload sample video; upload character images / voice
  samples; consent confirm endpoint; `POST /projects/{id}/chat`; project status, versions,
  revert; job list; SSE stream `/projects/{id}/events` for job finished notifications; static
  media serving from data_dir (path-traversal safe).
- Tests: director loop with a scripted FakeProvider calling tools; API tests with TestClient.

## T10 — Minimal web UI
- One static page served by FastAPI (`api/static/`, vanilla JS, no build step): chat panel;
  upload sample video; asset library (create character/voice, confirm consent); shot grid showing
  current frame/video/audio per shot with version dropdown + revert; approve buttons for the three
  checkpoints; live job status via SSE; budget/spent display.
- `README.md`: full setup (venv, extras, `.env`, ComfyUI profiles, running `uvicorn`), and the
  user workflow from SPEC §6.
- Test: page is served; API endpoints the page uses exist (TestClient).

## After T10 — keep going (the user is away; do NOT stop to ask questions)

Continue with these, same ground rules (own commit each, suite green):

### T11 — Quality pass
- Add `ruff` (lint + format) and `mypy` (strict on `src/avd/state`, `jobs`, `engine`; normal
  elsewhere) to dev deps and config in pyproject. Fix all findings.
- Re-read SPEC.md end to end and the code; list every gap/mismatch in `docs/REVIEW.md`, then fix
  each one that is fixable without external services. Mark the rest as open.
- Add `scripts/check.ps1` and `scripts/check.sh` running ruff, mypy, pytest.

### T12 — Offline end-to-end demo that produces a real MP4
- `avd.demo` "local stub" backends that create **real media with ffmpeg only**: first frame =
  generated image with the shot's text drawn on it; image_to_video = still-image clip with a slow
  zoom for `duration_s`; tts = sine tone/silence of the right length (or Windows SAPI / `espeak`
  if available, optional); lipsync = passthrough mux of video + audio.
- `python -m avd.demo` builds a 3-shot demo project through the **real** rebuild engine and job
  queue with these backends, writes `data/demo/final.mp4` (with subtitles + AI label), then edits
  shot 2's line and rebuilds incrementally, printing which nodes re-ran and the cache hits.
- Integration test running the demo (skip if ffmpeg missing). If ffmpeg is missing on this
  machine, document how to install it in README and still make the code ready.

### T13 — CLI
- `avd` console script (typer or argparse): `init`, `project create/list/status`,
  `analyze <video>`, `asset character create`, `asset voice create`, `consent confirm`,
  `chat <project>` (REPL with the director), `rebuild <project>`, `versions`, `revert`,
  `jobs`, `worker --lane gpu|api|cpu`, `serve` (uvicorn). Tests for the non-LLM commands.

### T14 — Rented Linux GPU deployment kit (`deploy/`)
- `deploy/gpu-server/setup.sh`: idempotent Ubuntu setup for a fresh rented GPU box: checks
  `nvidia-smi`, installs Python/ffmpeg/git, installs ComfyUI in a venv with a PyTorch build that
  supports RTX 50-series (Blackwell, CUDA 12.8+), installs a configurable list of custom nodes,
  downloads models from a `models.yaml` manifest (URL, target dir, sha256 optional; HF token via
  env), creates a systemd unit (or tmux fallback) for ComfyUI bound to 127.0.0.1.
- `deploy/gpu-server/README.md`: how to rent, run setup, connect from the laptop via SSH tunnel
  (`ssh -L 8188:127.0.0.1:8188 ...`), set the `remote` ComfyUI profile, and shut down to stop
  paying. Never expose ComfyUI publicly.
- `deploy/Dockerfile` for the app itself (not the GPU box) + `docker-compose.yml` (app + worker).
- Lint shell scripts with `bash -n`; no test needs a GPU.

### T15 — Model research doc (use the web if available; otherwise mark as "unverified")
- `docs/MODELS.md`: for image-to-video, voice cloning/TTS (Chinese + English), and lip sync:
  2–4 current open-source candidates each, with VRAM needs (12 GB laptop vs 24/48 GB rented),
  RTX 50-series compatibility notes, ComfyUI node availability, license (commercial use?), and a
  recommended default per tier. Cite sources with links and access date. Then fill
  `models.yaml` and the workflow README with the recommended defaults.

### T16 — Observability & robustness
- Structured logging (JSON option) with project/job ids; `/health` and `/metrics`-style JSON
  endpoint (queue depth per lane, running jobs, spend per project).
- Graceful shutdown of workers; job timeouts per job_type (configurable) that mark jobs failed
  and are retried per max_attempts.

If all of that is done and time remains, keep improving test coverage of the riskiest paths
(rebuild engine, director tools, API upload handling) and log what you did in PROGRESS.md.

## Done when
All milestones (T03b … T16) committed in order, the whole test suite + ruff + mypy green,
`docs/PROGRESS.md`, `docs/DECISIONS.md`, `docs/REVIEW.md` up to date, `.env.example` lists every
variable. Final message: a summary per milestone (commit hash, tests, gaps), the decisions you
made, and anything that needs the user (API keys, GPU box, real ComfyUI workflows).
