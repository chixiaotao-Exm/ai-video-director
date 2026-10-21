# T17 — Full web UI, pure white style

Work only in D:\code\ai-video-director. Same ground rules as GOAL-T04-T16.md (own commits,
suite + ruff + mypy green, no network/paid calls in tests, decisions → docs/DECISIONS.md).
The user wants **the whole program operable from the browser** with a **pure white, minimal**
look. Today's UI (T10, `src/avd/api/static/`) is a dark single project page; replace it.

## 1. Visual style — "pure white"

- Background `#FFFFFF` everywhere (page, panels, inputs, modals). No dark mode, no gradients,
  no shadows except a very faint one on modals/popovers. No colored panels.
- Text `#111111`; secondary text `#6B6B6B`; borders/dividers `#E8E8E8` (1px); hover background
  `#F7F7F7`; selected/active item: black text + 2px black left/bottom indicator.
- Primary button: black background, white text. Secondary: white with `#E8E8E8` border.
  Danger: white with red text (`#C62828`) only — no red fills.
- Status uses small dots/text, low saturation: running `#1565C0`, done `#2E7D32`,
  failed `#C62828`, queued/dirty `#9E9E9E`. That's the only color on the page besides media.
- Typography: system stack (`-apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
  sans-serif`), 14px base, generous whitespace (8px spacing scale), radius 8px, thin icons (inline
  SVG, stroke 1.5, no icon font / CDN).
- All colors/spacing as CSS custom properties in one `tokens.css`.
- Responsive: works at 1440px and 390px wide (sidebar collapses to a top menu on mobile).
- UI language: Chinese (keep strings in one `i18n/zh.js` map so English can be added later).

## 2. Structure (no build step; vanilla JS ES modules + hash router)

App shell: left sidebar nav + top bar (current project switcher, budget pill, connection dot).
Pages:

1. **工作台 Dashboard** — recent projects (cards with thumbnail of final/first frame, status,
   spend), "新建项目", system status summary (ComfyUI reachable?, workers running?, API keys
   set?) with links to fix each.
2. **项目 Project** (`#/projects/:id`) — tabs:
   - 概览: the 6-step workflow from SPEC §6 as a vertical stepper, each step showing state and its
     action button; the three checkpoints (script, first frames, final) with approve buttons.
   - 导演对话: chat with the director (streaming or polling), tool calls shown as compact
     collapsible rows, job progress inline.
   - 分镜: storyboard table; inline edit of line / visual prompt / camera / emotion / duration
     per shot (calls the existing update_shot path → dirty propagation), dirty badges.
   - 镜头: shot grid — first frame, video, audio players per shot; version dropdown + revert;
     "regenerate this shot".
   - 成片: final video player, download, version history, AI-label note.
   - 分析: structure template viewer for the sample video.
3. **素材库 Assets** — characters (reference images per view, appearance prompt, character sheet
   generation) and voices (samples with audio player, engine, defaults); consent status with
   a clear consent dialog (who confirms + checkbox text); assets can't be used until consented.
4. **任务队列 Jobs** — live table of jobs across projects (lane, type, model_key, status,
   attempts, duration, error expandable), filters, cancel queued, retry failed (add a
   `POST /jobs/{id}/retry` endpoint if missing). Worker status per lane.
5. **费用 Costs** — ledger table (per project / role / model), totals, per-project budget edit.
6. **设置 Settings** — everything currently only in `.env`:
   - API providers: Anthropic / OpenAI / Gemini — base URL, API key, model per role, prices.
     Keys are **write-only** in the UI: show only "已设置 ••••abcd" + replace/clear, never return
     full keys from any endpoint. A "测试连接" button per provider (makes one tiny real call —
     user-triggered only, never in tests).
   - ComfyUI profiles (local / remote URL), active profile, "测试连接" (GET /system_stats).
   - Budget defaults, confirm threshold, data_dir (read-only display), log format.
   - Persist to a local `data/settings.json` (gitignored) layered over env: env < settings file
     < explicit overrides. Document precedence in README. File written atomically.
7. **系统 System** — health, metrics, versions, FFmpeg found?, workers, recent log lines.

## 3. Backend work needed for the UI

- `avd serve --with-workers` (and make it the default for `avd serve` with a `--no-workers`
  flag): start gpu/api/cpu worker processes alongside the server and stop them on shutdown, so
  the whole program starts with one command. Worker start/stop/status endpoints for the System
  page.
- Settings endpoints: `GET/PUT /settings` (masked secrets), `POST /settings/test/{provider}`.
- Jobs endpoints across projects: list with filters, retry. Ledger endpoints with aggregation.
- Thumbnails for project cards (reuse existing frame/final outputs; no new generation).
- **Security for a localhost app:** reject requests whose `Host` isn't localhost/127.0.0.1 and
  state-changing requests whose `Origin` isn't the app's own origin (DNS-rebinding / CSRF from
  other websites). Settings and media endpoints included. Tests for both.

## 4. Verification

- API tests (TestClient) for every new endpoint, secret masking, settings precedence, host/
  origin checks, worker supervisor start/stop with fake workers.
- `node --check` every JS module if Node is available (it is on this machine); add it to
  `scripts/check.*`.
- **Screenshots:** start the server against the offline demo data (`python -m avd.demo` output)
  and capture every page with headless Microsoft Edge
  (`msedge --headless=new --screenshot=... --window-size=1440,900 <url>`, and 390,844 for
  mobile). Save to `docs/ui/<page>-desktop.png` / `-mobile.png`. Look at them yourself and fix
  layout problems before committing.
- Update README: "启动：`avd serve` 然后打开 http://127.0.0.1:8000".

Commits: split sensibly, e.g. `T17a: settings store and secure local API`,
`T17b: worker supervisor in serve`, `T17c: white web UI shell and pages`, `T17d: UI polish and
screenshots`. Final report: commits, test counts, screenshot paths, decisions, open issues.
