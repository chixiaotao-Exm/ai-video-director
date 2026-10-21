# T03 — Persistent job queue (+ small T02 review fixes)

Work only in D:\code\ai-video-director. Re-read AGENTS.md and SPEC.md section 3.

## Part A — T02 review fixes (do first, separate commit)

1. **Consent bypass:** `ProjectRepo.create_project` must not accept `character_id` / `voice_id`.
   The only way to link assets to a project is `attach_assets` (which enforces consent).
   Add a test proving there is no other path.
2. Move `ConsentRequiredError` and `require_usable` into `src/avd/state/consent.py` so `state`
   no longer imports from `assets` (remove the function-level import in repo.py). `assets`
   re-exports them for convenience.
3. `create_voice` must require at least one sample. Test it.

Commit: `T02b: close consent bypass, fix module layering`.

## Part B — Job queue

Implement `src/avd/jobs/`:

1. **Model** (`jobs/models.py`, same SQLAlchemy Base): `Job` with id, project_id (nullable),
   node_id (nullable), `lane` (`gpu` | `api` | `cpu`), `job_type` (str, e.g. `image_to_video`,
   `tts`, `lipsync`, `image_gen`, `ffmpeg`), `model_key` (str, used to group GPU jobs that need
   the same loaded model), `payload` (JSON), `status` (`queued` | `running` | `succeeded` |
   `failed` | `cancelled`), `result` (JSON), `error` (text), `attempts`, `max_attempts`
   (default 3), `priority` (int, higher first), created_at, started_at, finished_at.
2. **Queue API** (`jobs/queue.py`, `JobQueue(session_factory)`):
   - `submit(lane, job_type, payload, *, model_key=None, project_id=None, node_id=None,
     priority=0, max_attempts=3) -> job_id` — non-blocking, just persists.
   - `get(job_id)`, `list(project_id=None, status=None)`, `cancel(job_id)` (only queued jobs).
   - `claim_next(lane, current_model_key=None) -> Job | None` — atomically marks one job
     `running`. Ordering: highest priority first; then, **for the gpu lane, prefer jobs whose
     `model_key == current_model_key`** (avoid model swaps); then oldest first.
   - `complete(job_id, result)`, `fail(job_id, error)` — on fail, if attempts < max_attempts the
     job goes back to `queued`, else `failed`.
   - `recover_stale()` — on startup, jobs left `running` (crash) go back to `queued`.
3. **Worker** (`jobs/worker.py`):
   - Handler registry: `register(job_type, handler)`; handler is `Callable[[Job], dict]`.
   - `Worker(queue, lane, registry)` with `run_once()` (claim + execute one job, returns whether
     it did work) and `run_forever(stop_event, poll_interval)` for a background thread.
   - The **gpu lane has exactly one worker** (serial). The `api` lane may run N workers.
   - The worker remembers the last `model_key` it ran and passes it to `claim_next`.
   - Exceptions in handlers → `queue.fail` with the traceback text; unknown job_type → fail
     without retry.
   - Optional `on_finished(job)` callback, so the director can be notified later.
4. **Tests** (`tests/jobs/`, in-memory or tmp sqlite, no real GPU — handlers are fakes):
   submit/claim/complete; priority order; gpu model_key grouping (queue A,B,A with current=A →
   runs A,A,B); retry until max_attempts then failed; cancel only queued; recover_stale after a
   simulated crash; unknown job_type fails without retry; two api workers in threads never claim
   the same job (use a file-based sqlite in tmp_path for this one).

Note on SQLite concurrency: use `BEGIN IMMEDIATE` (or an equivalent atomic
`UPDATE ... WHERE status='queued'` + rowcount check) in `claim_next` so two workers can't claim
the same job.

Commit: `T03: persistent job queue and workers`.
All tests must pass. Report: files changed, test output, open questions.
