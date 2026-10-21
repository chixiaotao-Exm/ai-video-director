"""Human approvals tied to the exact project content that was reviewed."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.jobs.models import Job, JobStatus
from avd.state.models import Base, NodeKind, NodeStatus, Project
from avd.state.paths import relative_path

STAGES = ("structure", "script", "frames", "final")
_STAGE_KIND = {
    "structure": NodeKind.ANALYSIS,
    "script": NodeKind.SCRIPT,
    "frames": NodeKind.SHOT_FRAME,
    "final": NodeKind.FINAL,
}
PERFORMANCE_STAGES = ("performance_inputs", "performance_preview", "performance_final")


def generation_review_stage(project: Project, node_kind: NodeKind) -> str | None:
    if project.mode == "performance_remix":
        if node_kind in {NodeKind.PERFORMANCE_PREPARE, NodeKind.VOCAL_SEPARATION}:
            return None
        if node_kind in {
            NodeKind.SCENE_REFERENCE,
            NodeKind.SINGING_VOICE,
            NodeKind.PERFORMANCE_MIX,
        }:
            return "performance_inputs"
        preparation = (project.performance_config or {}).get("preparation")
        duration = preparation.get("duration_s", 10) if isinstance(preparation, dict) else None
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and math.isfinite(duration)
            and 0 < duration <= 10
        ):
            # A short video preview must be generated before it can be reviewed.
            return "performance_inputs"
        return "performance_preview"
    return "script" if node_kind == NodeKind.SHOT_FRAME else "frames"


def _performance_fingerprint(project: Project, stage: str) -> str:
    if not project.performance_config:
        raise ReviewRequired("Configure performance inputs before review")
    config = project.performance_config
    assets = {"character": project.character_id, "voice": project.voice_id}
    if stage == "performance_inputs":
        return _hash(
            {
                "config": config,
                "assets": assets,
                "recipes": [
                    (n.id, {k: v for k, v in n.recipe.items() if not k.startswith("_")})
                    for n in sorted(project.nodes, key=lambda n: n.id)
                ],
            }
        )
    kinds = {
        NodeKind.SCENE_REFERENCE,
        NodeKind.SINGING_VOICE,
        NodeKind.MOTION_VIDEO,
        NodeKind.PERFORMANCE_SYNC,
        NodeKind.PERFORMANCE_MIX,
        NodeKind.PERFORMANCE_FINAL,
    }
    if config.get("lipsync"):
        kinds.add(NodeKind.PERFORMANCE_LIPSYNC)
    nodes = [n for n in project.nodes if n.kind in kinds]
    if {n.kind for n in nodes} != kinds or any(
        n.status != NodeStatus.CLEAN
        or not n.current_version_id
        or n.current_version is None
        or n.current_version.node_id != n.id
        for n in nodes
    ):
        raise ReviewRequired(f"The {stage} output is not ready for review")
    for node in nodes:
        if node.kind != NodeKind.PERFORMANCE_FINAL:
            continue
        assert node.current_version is not None
        outputs = node.current_version.output_paths
        video = outputs.get("video") if isinstance(outputs, dict) else None
        try:
            if not isinstance(video, str) or not video.strip():
                raise ValueError("Final video path is missing")
            path = relative_path(video)
            if PurePosixPath(path).suffix.lower() not in {".mp4", ".mov", ".mkv", ".webm"}:
                raise ValueError("Final output is not a video media path")
        except ValueError as exc:
            raise ReviewRequired(f"The {stage} output needs a relative final video path") from exc
    return _hash(
        {
            "review_schema": "performance_video_v1",
            "stage": stage,
            "config": config,
            "assets": assets,
            "versions": sorted((n.id, n.current_version_id) for n in nodes),
        }
    )


class ReviewRequired(RuntimeError):
    """A human must review the current version before work can proceed."""


class Review(Base):
    __tablename__ = "director_reviews"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    stage: Mapped[str] = mapped_column(String(32), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    approved_by: Mapped[str] = mapped_column(String(255), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def _stage_fingerprint(session: Session, project: Project, stage: str) -> str:
    if project.mode == "performance_remix":
        if stage not in PERFORMANCE_STAGES:
            raise ValueError(f"Unknown performance review stage: {stage}")
        return _performance_fingerprint(project, stage)
    if stage not in STAGES:
        raise ValueError(f"Unknown review stage: {stage}")
    kinds = {_STAGE_KIND[name] for name in STAGES[: STAGES.index(stage) + 1]}
    nodes = sorted((node for node in project.nodes if node.kind in kinds), key=lambda n: n.id)
    if not all(any(node.kind == kind for node in nodes) for kind in kinds):
        raise ReviewRequired(f"The {stage} output is not ready for review")
    if any(node.status != NodeStatus.CLEAN or not node.current_version_id for node in nodes):
        raise ReviewRequired(f"The {stage} output is not ready for review")
    authoring = ["analyze_video"]
    if stage != "structure":
        authoring.append("write_script")
    pending = session.scalar(
        select(Job.id)
        .where(
            Job.project_id == project.id,
            Job.job_type.in_(authoring),
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
        .limit(1)
    )
    if pending is not None:
        raise ReviewRequired(f"The {stage} output has a pending authoring job")
    return _hash([(node.id, node.current_version_id) for node in nodes])


def _save(
    session: Session,
    project: Project,
    stage: str,
    fingerprint: str,
    approved_by: str,
    details: dict[str, Any] | None = None,
) -> Review:
    actor = approved_by.strip()
    if not actor:
        raise ValueError("approved_by is required")
    review = session.get(Review, (project.id, stage))
    if review is None:
        review = Review(project_id=project.id, stage=stage)
        session.add(review)
    review.fingerprint = fingerprint
    review.details = details or {}
    review.approved_by = actor
    review.approved_at = datetime.now(timezone.utc)
    session.flush()
    return review


def approve(session: Session, project: Project, stage: str, approved_by: str) -> dict[str, Any]:
    """Record a human review. The API owns authorization and transaction commit."""
    review = _save(
        session, project, stage, _stage_fingerprint(session, project, stage), approved_by
    )
    return {
        "stage": stage,
        "approved": True,
        "approved_by": review.approved_by,
        "approved_at": review.approved_at.isoformat(),
    }


def require_review(session: Session, project: Project, stage: str) -> None:
    fingerprint = _stage_fingerprint(session, project, stage)
    review = session.get(Review, (project.id, stage))
    if review is None or review.fingerprint != fingerprint:
        raise ReviewRequired(f"Human approval required for the current {stage} output")


def review_status(session: Session, project: Project) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for stage in PERFORMANCE_STAGES if project.mode == "performance_remix" else STAGES:
        review = session.get(Review, (project.id, stage))
        try:
            require_review(session, project, stage)
        except ReviewRequired as exc:
            result[stage] = {"approved": False, "reason": str(exc)}
        else:
            assert review is not None
            result[stage] = {
                "approved": True,
                "approved_by": review.approved_by,
                "approved_at": review.approved_at.isoformat(),
            }
    return result


def _budget_fingerprint(project: Project) -> str:
    # The engine adds _inputs during scheduling; that bookkeeping is not an edit.
    return _hash(
        {
            "budget_limit": project.budget_limit,
            "character_id": project.character_id,
            "voice_id": project.voice_id,
            "nodes": [
                {
                    "id": node.id,
                    "recipe": {
                        key: value
                        for key, value in node.recipe.items()
                        if key not in {"_inputs", "_execution"}
                    },
                    "version": node.current_version_id
                    if node.kind in {NodeKind.ANALYSIS, NodeKind.SCRIPT}
                    else None,
                }
                for node in sorted(project.nodes, key=lambda node: node.id)
            ],
        }
    )


def approve_budget(
    session: Session,
    project: Project,
    amount: float,
    approved_by: str,
) -> dict[str, Any]:
    """Authorize a bounded amount for the present recipes through the human API."""
    if not math.isfinite(amount) or amount < 0:
        raise ValueError("Budget approval amount must be finite and non-negative")
    _save(
        session,
        project,
        "budget",
        _budget_fingerprint(project),
        approved_by,
        {"amount": amount, "spent": project.spent or 0.0},
    )
    return {"stage": "budget", "approved": True, "amount": amount}


def budget_is_confirmed(session: Session, project: Project, estimated_cost: float) -> bool:
    if not math.isfinite(estimated_cost) or estimated_cost < 0:
        raise ValueError("Estimated cost must be finite and non-negative")
    review = session.get(Review, (project.id, "budget"))
    if review is None or review.fingerprint != _budget_fingerprint(project):
        return False
    spent_since = max(0.0, (project.spent or 0.0) - float(review.details["spent"]))
    return spent_since + estimated_cost <= float(review.details["amount"]) + 1e-9


__all__ = [
    "Review",
    "ReviewRequired",
    "STAGES",
    "approve",
    "require_review",
    "review_status",
    "approve_budget",
    "budget_is_confirmed",
]
