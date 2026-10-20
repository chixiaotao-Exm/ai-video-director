# T02 — Asset library (characters & voices, consent gate)

Work only in D:\code\ai-video-director. Re-read AGENTS.md and SPEC.md sections 2.1 and 7.
Implement only what is below.

1. `src/avd/config.py`: a Pydantic `Settings` (pydantic-settings is fine, add the dependency)
   with `data_dir` (default `./data`) and `database_url` (default sqlite file in data_dir).
   Env prefix `AVD_`.
2. `src/avd/assets/storage.py`: `AssetStorage(data_dir)` that copies an incoming file into
   `data_dir/assets/{characters|voices}/{asset_id}/` with a content-hash filename, and returns the
   **relative** path stored in the DB. Never overwrite an existing file. Reject files outside
   allowed extensions (images: png/jpg/jpeg/webp; audio: wav/mp3/flac/m4a).
3. `src/avd/assets/library.py`: `AssetLibrary(session, storage)` with
   - `create_character(name, reference_images: dict[view, path], appearance_prompt, lora_path=None)`
     — views limited to `front|side|half|full`, `front` required.
   - `create_voice(name, sample_paths, engine, engine_voice_id=None, model_path=None, default_params=None)`
   - `confirm_consent(asset, confirmed_by: str)` — sets `consent_confirmed=True` and records who/when
     (add `consent_confirmed_by`, `consent_confirmed_at` columns to both models).
   - `get_character`, `get_voice`, `list_characters`, `list_voices`, `update_character_prompt`.
   - `require_usable(asset)` — raises `ConsentRequiredError` when consent is not confirmed.
4. Wire the consent gate into project state: `ProjectRepo.attach_assets(project, character, voice)`
   calls `require_usable` for each and refuses unconsented assets.
5. Tests in `tests/assets/` (use `tmp_path`, tiny generated files): storage copies + dedups by
   hash, rejects bad extensions, DB holds relative paths only; front image required; consent gate
   blocks `attach_assets` until confirmed; consent records who/when; one character reused across
   two projects.

All tests (old + new) must pass with the project venv. Commit as `T02: asset library and consent gate`.
Report: files changed, test output, open questions.
