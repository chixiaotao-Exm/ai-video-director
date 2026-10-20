"""SQLAlchemy models for the project's persistent state.

The state database is intentionally the source of truth for generated content.  A
node may have many immutable :class:`Version` rows, while ``current_version``
points at the version currently selected for that node.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .paths import relative_path, relative_paths


def _new_id() -> str:
    """Return a compact, database-friendly identifier."""

    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Declarative base shared by all state models."""


class NodeKind(str, Enum):
    """Kinds of work represented by a project node."""

    ANALYSIS = "analysis"
    SCRIPT = "script"
    SHOT_FRAME = "shot_frame"
    SHOT_VIDEO = "shot_video"
    VOICEOVER = "voiceover"
    LIPSYNC = "lipsync"
    SUBTITLES = "subtitles"
    FINAL = "final"


class NodeStatus(str, Enum):
    """Current execution state of a node."""

    CLEAN = "clean"
    DIRTY = "dirty"
    RUNNING = "running"
    FAILED = "failed"


def _enum_values(enum_type: type[Enum]) -> list[str]:
    """Store enum values (rather than Python member names) in SQLite."""

    return [str(member.value) for member in enum_type]


node_dependencies = Table(
    "node_dependencies",
    Base.metadata,
    Column(
        "node_id",
        String(36),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "depends_on_id",
        String(36),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Character(Base):
    """A reusable character in the global asset library."""

    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_images: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    fixed_appearance_prompt: Mapped[str | None] = mapped_column(String, nullable=True)
    lora_path: Mapped[str | None] = mapped_column(String, nullable=True)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    projects: Mapped[list[Project]] = relationship("Project", back_populates="character")

    @property
    def front_image(self) -> str | None:
        """Convenience accessor for the conventional front reference image."""

        return (self.reference_images or {}).get("front")

    @property
    def side_image(self) -> str | None:
        return (self.reference_images or {}).get("side")

    @property
    def half_image(self) -> str | None:
        return (self.reference_images or {}).get("half")

    @property
    def full_image(self) -> str | None:
        return (self.reference_images or {}).get("full")


class Voice(Base):
    """A reusable, consented voice in the global asset library."""

    __tablename__ = "voices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_audio_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    engine: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engine_voice_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_path: Mapped[str | None] = mapped_column(String, nullable=True)
    default_params: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    consent_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    projects: Mapped[list[Project]] = relationship("Project", back_populates="voice")


class Project(Base):
    """A video project and its generation graph."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # "remake": an original video in a reference's style; "anime_sequel": the next episode.
    mode: Mapped[str] = mapped_column(String(32), nullable=False, default="remake")
    source_video_path: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    character_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("characters.id", ondelete="SET NULL"), nullable=True
    )
    voice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("voices.id", ondelete="SET NULL"), nullable=True
    )
    budget_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    spent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )

    character: Mapped[Character | None] = relationship("Character", back_populates="projects")
    voice: Mapped[Voice | None] = relationship("Voice", back_populates="projects")
    nodes: Mapped[list[Node]] = relationship(
        "Node",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Node.id",
    )


class Node(Base):
    """A generated or deterministic unit in a project's dependency graph."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[NodeKind] = mapped_column(
        SAEnum(
            NodeKind,
            name="node_kind",
            native_enum=False,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
    )
    shot_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recipe: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[NodeStatus] = mapped_column(
        SAEnum(
            NodeStatus,
            name="node_status",
            native_enum=False,
            values_callable=_enum_values,
            validate_strings=True,
        ),
        nullable=False,
        default=NodeStatus.DIRTY,
    )
    current_version_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("versions.id", ondelete="SET NULL"),
        nullable=True,
    )

    project: Mapped[Project] = relationship("Project", back_populates="nodes")
    depends_on: Mapped[list[Node]] = relationship(
        "Node",
        secondary=node_dependencies,
        primaryjoin=lambda: Node.id == node_dependencies.c.node_id,
        secondaryjoin=lambda: Node.id == node_dependencies.c.depends_on_id,
        back_populates="dependents",
        lazy="selectin",
    )
    dependents: Mapped[list[Node]] = relationship(
        "Node",
        secondary=node_dependencies,
        primaryjoin=lambda: Node.id == node_dependencies.c.depends_on_id,
        secondaryjoin=lambda: Node.id == node_dependencies.c.node_id,
        back_populates="depends_on",
        lazy="selectin",
    )
    versions: Mapped[list[Version]] = relationship(
        "Version",
        back_populates="node",
        foreign_keys="Version.node_id",
        cascade="all, delete-orphan",
        order_by="Version.number",
    )
    current_version: Mapped[Version | None] = relationship(
        "Version",
        foreign_keys=[current_version_id],
        post_update=True,
        uselist=False,
    )


class Version(Base):
    """An immutable snapshot of a node's recipe and generated outputs."""

    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("node_id", "number", name="uq_versions_node_number"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    recipe_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_paths: Mapped[dict[str, Any] | list[Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    recipe_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now
    )
    note: Mapped[str | None] = mapped_column(String, nullable=True)

    node: Mapped[Node] = relationship("Node", back_populates="versions", foreign_keys=[node_id])


@event.listens_for(Version, "before_update")
def _prevent_version_update(mapper: Any, connection: Any, target: Version) -> None:
    """Version rows are immutable once persisted."""

    state = inspect(target)
    changed = [
        attribute.key
        for attribute in state.mapper.column_attrs
        if state.attrs[attribute.key].history.has_changes()
    ]
    if changed:
        raise ValueError(f"Version rows are immutable; changed columns: {', '.join(changed)}")


@event.listens_for(Version, "before_delete")
def _prevent_version_delete(mapper: Any, connection: Any, target: Version) -> None:
    raise ValueError("Version rows are immutable and cannot be deleted")


@event.listens_for(Character, "before_insert")
@event.listens_for(Character, "before_update")
def _validate_character_paths(mapper: Any, connection: Any, target: Character) -> None:
    target.reference_images = relative_paths(target.reference_images or {})
    if target.lora_path is not None:
        target.lora_path = relative_path(target.lora_path)


@event.listens_for(Voice, "before_insert")
@event.listens_for(Voice, "before_update")
def _validate_voice_paths(mapper: Any, connection: Any, target: Voice) -> None:
    target.sample_audio_paths = relative_paths(target.sample_audio_paths or [])
    if target.model_path is not None:
        target.model_path = relative_path(target.model_path)


@event.listens_for(Project, "before_insert")
@event.listens_for(Project, "before_update")
def _validate_source_path(mapper: Any, connection: Any, target: Project) -> None:
    if target.source_video_path is not None:
        target.source_video_path = relative_path(target.source_video_path)


@event.listens_for(Version, "before_insert")
def _validate_output_paths(mapper: Any, connection: Any, target: Version) -> None:
    target.output_paths = relative_paths(
        target.output_paths if target.output_paths is not None else {}
    )


__all__ = [
    "Base",
    "Character",
    "Node",
    "NodeKind",
    "NodeStatus",
    "Project",
    "Version",
    "Voice",
    "node_dependencies",
]
