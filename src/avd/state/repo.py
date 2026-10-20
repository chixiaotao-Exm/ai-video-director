"""Persistence and dependency-graph operations for project state.

The repository deliberately keeps all graph and versioning behaviour in one
place.  Callers work with SQLAlchemy model instances; the repository flushes
the current transaction after writes but does not commit it, leaving
transaction ownership to the caller.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Node, NodeKind, NodeStatus, Project, Version


_KIND_ORDER: tuple[str, ...] = (
    "analysis",
    "script",
    "shot_frame",
    "shot_video",
    "voiceover",
    "lipsync",
    "subtitles",
    "final",
)
_KIND_RANK = {kind: index for index, kind in enumerate(_KIND_ORDER)}


def _canonical_json(value: Any) -> str:
    """Serialize JSON-compatible values deterministically for hashing."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _recipe_hash(recipe: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(_canonical_json(recipe or {}).encode("utf-8")).hexdigest()


def _node_sort_key(node: Node) -> tuple[int, int, str, str]:
    kind = node.kind.value
    shot_index = node.shot_index if node.shot_index is not None else -1
    return (_KIND_RANK.get(kind, len(_KIND_ORDER)), shot_index, kind, str(node.id))


class ProjectRepo:
    """Repository for projects, nodes, dependencies, and immutable versions."""

    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Project and graph construction
    # ------------------------------------------------------------------
    def create_project(
        self,
        title: str,
        source_video_path: str | None = None,
        analysis_result: Mapping[str, Any] | None = None,
        character_id: str | None = None,
        voice_id: str | None = None,
        budget_limit: float = 0.0,
    ) -> Project:
        """Create and flush a project."""

        project = Project(
            title=title,
            source_video_path=source_video_path,
            analysis_result=(
                copy.deepcopy(dict(analysis_result))
                if analysis_result is not None
                else None
            ),
            character_id=character_id,
            voice_id=voice_id,
            budget_limit=budget_limit,
        )
        self.session.add(project)
        self.session.flush()
        return project

    def add_node(
        self,
        project: Project,
        kind: NodeKind | str,
        shot_index: int | None = None,
        recipe: Mapping[str, Any] | None = None,
        depends_on: Iterable[Node] | None = None,
    ) -> Node:
        """Add a node and its prerequisite edges to a project."""

        prerequisites = list(depends_on or ())
        node = Node(
            project=project,
            project_id=project.id,
            kind=NodeKind(kind),
            shot_index=shot_index,
            recipe=copy.deepcopy(dict(recipe or {})),
            status=NodeStatus.DIRTY,
        )
        self.session.add(node)
        self.session.flush()
        node.depends_on.extend(prerequisites)
        self.session.flush()
        return node

    def create_shot_graph(self, project: Project, shot_count: int) -> dict[str, Any]:
        """Create the dependency graph described in SPEC section 2.2.

        The return value is a convenient mapping containing the shared script,
        per-shot node lists, and final node.  All nodes are also attached to the
        supplied ``project`` relationship.
        """

        if shot_count < 0:
            raise ValueError("shot_count must be non-negative")

        script = self.add_node(project, NodeKind.SCRIPT, recipe={})
        shot_frames: list[Node] = []
        shot_videos: list[Node] = []
        voiceovers: list[Node] = []
        lipsyncs: list[Node] = []
        subtitles: list[Node] = []

        for shot_index in range(1, shot_count + 1):
            frame = self.add_node(
                project,
                NodeKind.SHOT_FRAME,
                shot_index=shot_index,
                recipe={"shot_index": shot_index},
                depends_on=[script],
            )
            video = self.add_node(
                project,
                NodeKind.SHOT_VIDEO,
                shot_index=shot_index,
                recipe={"shot_index": shot_index},
                depends_on=[frame],
            )
            voiceover = self.add_node(
                project,
                NodeKind.VOICEOVER,
                shot_index=shot_index,
                recipe={"shot_index": shot_index},
                depends_on=[script],
            )
            lipsync = self.add_node(
                project,
                NodeKind.LIPSYNC,
                shot_index=shot_index,
                recipe={"shot_index": shot_index},
                depends_on=[video, voiceover],
            )
            subtitle = self.add_node(
                project,
                NodeKind.SUBTITLES,
                shot_index=shot_index,
                recipe={"shot_index": shot_index},
                depends_on=[lipsync],
            )
            shot_frames.append(frame)
            shot_videos.append(video)
            voiceovers.append(voiceover)
            lipsyncs.append(lipsync)
            subtitles.append(subtitle)

        final = self.add_node(
            project,
            NodeKind.FINAL,
            recipe={},
            depends_on=subtitles,
        )
        self.session.flush()
        return {
            "script": script,
            "shot_frame": shot_frames,
            "shot_video": shot_videos,
            "voiceover": voiceovers,
            "lipsync": lipsyncs,
            "subtitles": subtitles,
            "final": final,
        }

    # ------------------------------------------------------------------
    # Dirty propagation and versioning
    # ------------------------------------------------------------------
    @staticmethod
    def _set_dirty(node: Node) -> None:
        node.status = NodeStatus.DIRTY

    @staticmethod
    def _dependents(node: Node) -> list[Node]:
        return list(node.dependents)

    def _mark_dependents_dirty(self, node: Node) -> None:
        """Mark every transitive dependent dirty, visiting each node once."""

        seen: set[str] = set()
        stack = list(self._dependents(node))
        while stack:
            dependent = stack.pop()
            marker = str(dependent.id)
            if marker in seen:
                continue
            seen.add(marker)
            self._set_dirty(dependent)
            stack.extend(self._dependents(dependent))

    def update_recipe(
        self,
        node: Node,
        patch: Mapping[str, Any],
        note: str | None = None,
    ) -> Node:
        """Merge a recipe patch, mark the node dirty, and propagate dirtiness."""

        if not isinstance(patch, Mapping):
            raise TypeError("patch must be a mapping")
        recipe = copy.deepcopy(dict(node.recipe or {}))
        recipe.update(copy.deepcopy(dict(patch)))
        node.recipe = recipe
        self._set_dirty(node)
        self._mark_dependents_dirty(node)
        self.session.flush()
        # ``note`` belongs to Version rows.  It is intentionally not stored
        # on the mutable Node, but accepting it keeps the API aligned with the
        # director tool's update call.
        _ = note
        return node

    def record_version(
        self,
        node: Node,
        outputs: Any,
        cost: float = 0.0,
        note: str | None = None,
    ) -> Version:
        """Record an immutable version for ``node`` and make it current."""

        previous = self.session.scalar(
            select(Version)
            .where(Version.node_id == node.id)
            .order_by(Version.number.desc())
            .limit(1)
        )
        number = (previous.number if previous is not None else 0) + 1
        snapshot = copy.deepcopy(dict(node.recipe or {}))
        version = Version(
            node=node,
            node_id=node.id,
            number=number,
            recipe_snapshot=snapshot,
            output_paths=copy.deepcopy(outputs),
            recipe_hash=_recipe_hash(snapshot),
            cost=cost,
            note=note,
        )
        self.session.add(version)
        self.session.flush()

        # Assign the relationship when available; setting the FK directly is
        # a fallback for minimal model implementations.
        node.current_version = version
        node.current_version_id = version.id
        node.status = NodeStatus.CLEAN
        self.session.flush()
        return version

    def find_cached_version(self, node: Node) -> Version | None:
        """Return the newest immutable version matching the current recipe."""

        recipe_hash = _recipe_hash(node.recipe or {})
        return self.session.scalar(
            select(Version)
            .where(Version.node_id == node.id, Version.recipe_hash == recipe_hash)
            .order_by(Version.number.desc())
            .limit(1)
        )

    # ------------------------------------------------------------------
    # Rebuild planning
    # ------------------------------------------------------------------
    @staticmethod
    def _prerequisites(node: Node) -> list[Node]:
        return list(node.depends_on)

    def plan_rebuild(self, project: Project) -> list[Node]:
        """Return dirty nodes in deterministic topological order.

        The complete project graph is checked for cycles first, including clean
        nodes.  This prevents silently producing a partial plan for malformed
        state.  Only dirty nodes are returned after the check.
        """

        nodes = list(project.nodes)
        nodes.sort(key=_node_sort_key)
        if not nodes:
            return []

        by_id = {str(node.id): node for node in nodes}
        indegree = {str(node.id): 0 for node in nodes}
        outgoing: dict[str, set[str]] = {str(node.id): set() for node in nodes}
        for node in nodes:
            node_id = str(node.id)
            for prerequisite in self._prerequisites(node):
                prerequisite_id = str(prerequisite.id)
                if prerequisite_id not in by_id:
                    # A dependency from another project is invalid state, but
                    # ignoring it here would make cycle/order errors obscure.
                    raise ValueError(
                        f"Node {node.id} depends on node {prerequisite.id} outside project {project.id}"
                    )
                outgoing[prerequisite_id].add(node_id)
                indegree[node_id] += 1

        ready = sorted(
            (by_id[node_id] for node_id, degree in indegree.items() if degree == 0),
            key=_node_sort_key,
        )
        ordered: list[Node] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for child_id in sorted(outgoing[str(current.id)], key=lambda item: _node_sort_key(by_id[item])):
                indegree[child_id] -= 1
                if indegree[child_id] == 0:
                    ready.append(by_id[child_id])
                    ready.sort(key=_node_sort_key)

        if len(ordered) != len(nodes):
            cycle_ids = sorted(
                node_id for node_id, degree in indegree.items() if degree > 0
            )
            raise ValueError(
                "Dependency cycle detected in project "
                f"{project.id}: {', '.join(cycle_ids)}"
            )

        return [
            node
            for node in ordered
            if node.status == NodeStatus.DIRTY
        ]

    # ------------------------------------------------------------------
    # Version restoration and inspection
    # ------------------------------------------------------------------
    def revert(self, node: Node, version_number: int) -> Version:
        """Restore a node's recipe/current version and dirty its dependents."""

        version = self.session.scalar(
            select(Version).where(
                Version.node_id == node.id,
                Version.number == version_number,
            )
        )
        if version is None:
            raise ValueError(
                f"Version {version_number} does not exist for node {node.id}"
            )

        node.recipe = copy.deepcopy(dict(version.recipe_snapshot or {}))
        node.current_version = version
        node.current_version_id = version.id
        node.status = NodeStatus.CLEAN
        self._mark_dependents_dirty(node)
        self.session.flush()
        return version

    def list_versions(self, node: Node) -> list[Version]:
        """List immutable versions in ascending version-number order."""

        return list(
            self.session.scalars(
                select(Version)
                .where(Version.node_id == node.id)
                .order_by(Version.number.asc())
            ).all()
        )


__all__ = ["ProjectRepo"]

