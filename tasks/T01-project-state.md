You are building the project in D:\code\ai-video-director (work only inside that folder).
First read D:\code\ai-video-director\AGENTS.md and D:\code\ai-video-director\SPEC.md.

# Task T01 — Project state, versioning, dirty propagation

Implement SPEC.md section 2 (project state) and nothing else.

1. Scaffold: `pyproject.toml` (package `avd`, src layout, deps: sqlalchemy>=2, pydantic>=2;
   dev: pytest), `src/avd/__init__.py`, `.gitignore` (Python, .env, data/, *.db, media files),
   `README.md` with setup + test commands. Run `git init` in the folder if it is not a repo yet.
2. `src/avd/state/models.py`: SQLAlchemy 2.0 models `Character`, `Voice`, `Project`, `Node`,
   `Version` with the fields from SPEC 2.1. Node `depends_on` as a proper association table
   (node_dependencies), not a JSON list. `NodeKind` and `NodeStatus` as enums.
3. `src/avd/state/db.py`: engine/session factory from a DB URL (default sqlite file under data_dir;
   tests use in-memory sqlite).
4. `src/avd/state/repo.py` — a `ProjectRepo` with:
   - `create_project(...)`, `add_node(project, kind, shot_index, recipe, depends_on)`
   - `create_shot_graph(project, shot_count)`: builds the per-shot graph from SPEC 2.2
     (script → shot_frame → shot_video; script → voiceover; shot_video + voiceover → lipsync →
     subtitles; all subtitles → final). One shared `script` node.
   - `update_recipe(node, patch: dict, note)`: merge patch into recipe, mark node dirty, propagate
     dirty to all transitive dependents.
   - `record_version(node, outputs, cost, note)`: creates an immutable Version (number = previous+1,
     stores recipe snapshot + `recipe_hash` = sha256 of canonical JSON), sets current, marks clean.
   - `find_cached_version(node)`: returns an existing version whose recipe_hash matches the node's
     current recipe, else None.
   - `plan_rebuild(project)`: dirty nodes in topological order (deterministic tie-break by
     kind order then shot_index). Raise a clear error on cycles.
   - `revert(node, version_number)`: restore that version's recipe + set current, mark dependents dirty.
   - `list_versions(node)`.
5. Tests in `tests/state/` covering at least: graph shape for 3 shots; editing voiceover of shot 2
   dirties exactly voiceover[2], lipsync[2], subtitles[2], final (shots 1 and 3 untouched);
   editing the script dirties everything downstream; plan_rebuild order is topological; version
   numbers increment and old versions are unchanged; cache hit after reverting a recipe change;
   revert marks dependents dirty; cycle detection.

Use a virtualenv at D:\code\ai-video-director\.venv for installs. Make `python -m pytest -q` pass.
End with a short report: files created, test result output, open questions.
