"""The director's live mind map: a reasoning tree kept alongside the conversation.

The director adds ideas, options, decisions and open questions as the chat goes.
Branches for the reference analysis and the storyboard are derived from project
state, so the map stays correct even when the director forgets to update it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.state.models import Base, NodeKind, Project

Kind = Literal["idea", "option", "decision", "question", "insight", "task"]
Status = Literal["open", "chosen", "rejected", "done"]

ROOT = "root"
REFERENCE = "reference"
SCRIPT = "script"
DERIVED_PARENTS = (ROOT, REFERENCE, SCRIPT)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MindMapNode(Base):
    __tablename__ = "mindmap_nodes"
    __table_args__ = (UniqueConstraint("project_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_key: Mapped[str] = mapped_column(String(64), nullable=False, default=ROOT)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="idea")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class MindMapItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    parent: str = Field(
        default=ROOT,
        min_length=1,
        max_length=64,
        description="Parent id: root, reference, script, shot-N, or one of your own ids.",
    )
    label: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
    kind: Kind = "idea"
    status: Status = "open"


def update(
    session: Session, project: Project, items: list[MindMapItem], remove: list[str]
) -> dict[str, Any]:
    """Upsert director nodes; removing a node also removes its director descendants."""
    existing = {
        node.key: node
        for node in session.scalars(select(MindMapNode).where(MindMapNode.project_id == project.id))
    }
    for item in items:
        if item.id in DERIVED_PARENTS or item.id.startswith("shot-"):
            raise ValueError(f"{item.id!r} is reserved for derived branches; choose another id")
        if item.parent == item.id:
            raise ValueError("A node cannot be its own parent")
        node = existing.get(item.id)
        if node is None:
            node = MindMapNode(project_id=project.id, key=item.id)
            session.add(node)
            existing[item.id] = node
        node.parent_key = item.parent
        node.label = item.label
        node.note = item.note
        node.kind = item.kind
        node.status = item.status
        node.updated_at = _now()
    doomed = set(remove)
    changed = True
    while changed:
        changed = False
        for key, node in existing.items():
            if key not in doomed and node.parent_key in doomed:
                doomed.add(key)
                changed = True
    for key in doomed:
        if key in existing:
            session.delete(existing.pop(key))
    session.flush()
    return {
        "nodes": len(existing),
        "updated": [item.id for item in items],
        "removed": sorted(doomed),
    }


def _derived(project: Project) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = [
        {"id": ROOT, "parent": None, "label": project.title, "kind": "root", "status": "open"}
    ]
    template = project.analysis_result
    if project.source_video_path or template:
        nodes.append(
            {
                "id": REFERENCE,
                "parent": ROOT,
                "label": "参考视频",
                "note": (template or {}).get("summary") or ("分析中…" if not template else None),
                "kind": "reference",
                "status": "done" if template else "open",
            }
        )
    if template:
        for key, label in (
            ("hook", "开场钩子"),
            ("pacing_notes", "节奏"),
            ("why_it_works", "为什么有效"),
        ):
            if template.get(key):
                nodes.append(
                    {
                        "id": f"reference.{key}",
                        "parent": REFERENCE,
                        "label": label,
                        "note": template[key],
                        "kind": "insight",
                        "status": "done",
                    }
                )
        for index, shot in enumerate(template.get("shots") or [], start=1):
            nodes.append(
                {
                    "id": f"reference.shot.{index}",
                    "parent": REFERENCE,
                    "label": f"镜头 {index} · {shot.get('shot_size', '')} · {shot.get('duration_s', '')}s",
                    "note": shot.get("visual_description"),
                    "kind": "insight",
                    "status": "done",
                }
            )
    script = next((node for node in project.nodes if node.kind == NodeKind.SCRIPT), None)
    recipe = (script.recipe or {}) if script is not None else {}
    shots = recipe.get("shots", [])
    if shots:
        nodes.append(
            {
                "id": SCRIPT,
                "parent": ROOT,
                "label": recipe.get("title") or "脚本",
                "kind": "decision",
                "status": "chosen",
            }
        )
        for shot in shots:
            nodes.append(
                {
                    "id": f"shot-{shot.get('index')}",
                    "parent": SCRIPT,
                    "label": f"镜头 {shot.get('index')} · {shot.get('camera', '')}",
                    "note": shot.get("line") or shot.get("visual_prompt"),
                    "kind": "task",
                    "status": "open",
                }
            )
    return nodes


def tree(session: Session, project: Project) -> dict[str, Any]:
    """Return derived and director nodes as a flat list; orphans attach to the root."""
    nodes = _derived(project)
    known = {node["id"] for node in nodes}
    rows = list(
        session.scalars(
            select(MindMapNode)
            .where(MindMapNode.project_id == project.id)
            .order_by(MindMapNode.created_at, MindMapNode.id)
        )
    )
    known.update(row.key for row in rows)
    for row in rows:
        nodes.append(
            {
                "id": row.key,
                "parent": row.parent_key if row.parent_key in known else ROOT,
                "label": row.label,
                "note": row.note,
                "kind": row.kind,
                "status": row.status,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        )
    latest = max((row.updated_at for row in rows if row.updated_at), default=None)
    return {"nodes": nodes, "updated_at": latest.isoformat() if latest else None}


__all__ = ["MindMapItem", "MindMapNode", "tree", "update"]
