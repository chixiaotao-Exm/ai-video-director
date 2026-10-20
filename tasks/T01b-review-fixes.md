# T01b — Review fixes for T01 (from the architect's review)

Work only in D:\code\ai-video-director. Keep the scope to these fixes.

1. **Bug: new nodes must start `dirty`, not `clean`.** A node with no version has never been
   built. Today a freshly created project returns an empty `plan_rebuild()`, so the first
   generation would never run. Fix `add_node` so new nodes are `DIRTY` (and model default too).
   Add a test: a new 3-shot project's `plan_rebuild` returns all 17 nodes, script first, final last.
   Update existing tests that relied on new nodes being clean (mark nodes clean explicitly via
   `record_version` in fixtures instead of poking `status`).
2. **Version numbers must be unique per node.** Add `UniqueConstraint("node_id", "number")` on
   `versions`. Test that inserting a duplicate raises `IntegrityError`.
3. **Enforce immutability of `Version`.** Add a SQLAlchemy `before_update` listener on `Version`
   that raises if any column changes. Test it.
4. **Simplify `repo.py`.** Remove defensive fallbacks for attributes that always exist on our
   models: the `dependencies` alias lookups, `hasattr(node, "current_version")` checks, and the
   `_kind_value` / `_status_value` coercion wrappers (use the enums directly; accept `NodeKind | str`
   by calling `NodeKind(kind)`). Remove the `Node.dependencies` property alias and the
   `Version.recipe` / `Version.outputs` aliases from models.py. Remove `**kwargs` from
   `create_project`. Behaviour must stay the same apart from item 1.
5. **Git.** Make the first commit of the current tree (exclude `.venv`, `data/`, logs).
   Message: `T01: project state, versioning, dirty propagation`.

Run `python -m pytest -q` with the project venv; all tests must pass.
Report: files changed, test output, anything you disagreed with.
