"""Persistent estimated cost ledger for model calls."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from avd.config import ModelPrice
from avd.jobs.models import Job  # noqa: F401 -- registers the referenced jobs table
from avd.state.models import Base, Project

from .budget import BudgetGuard

logger = logging.getLogger(__name__)


class CostEntry(Base):
    __tablename__ = "cost_entries"
    __table_args__ = (UniqueConstraint("job_id", "attempt", name="uq_cost_entries_job_attempt"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    est_cost: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class CostLedger:
    def __init__(self, session: Session, prices: Mapping[str, ModelPrice] | None = None) -> None:
        self.session = session
        self.prices = dict(prices or {})

    def estimate(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts cannot be negative")
        price = self.prices.get(f"{provider}/{model}", self.prices.get(model))
        if price is None:
            logger.warning("Unknown model price for %s/%s; estimated cost is zero", provider, model)
            return 0.0
        return (
            price.input_per_million * input_tokens + price.output_per_million * output_tokens
        ) / 1_000_000

    def record(
        self,
        project: Project,
        role: str,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> CostEntry:
        """Record one response and its cost atomically in the caller's transaction."""

        cost = self.estimate(provider, model, input_tokens, output_tokens)
        entry = CostEntry(
            project_id=project.id,
            role=role,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            est_cost=cost,
        )
        self.session.add(entry)
        BudgetGuard().record_spend(project, cost)
        self.session.flush()
        return entry

    def list(self, project_id: str) -> list[CostEntry]:
        return list(
            self.session.scalars(
                select(CostEntry)
                .where(CostEntry.project_id == project_id)
                .order_by(CostEntry.created_at, CostEntry.id)
            )
        )

    def record_attempt(
        self,
        project: Project,
        *,
        job_id: str,
        attempt: int,
        role: str,
        provider: str,
        model: str,
        cost: float,
    ) -> CostEntry:
        """Charge a durable executor attempt once in the caller's transaction."""
        if attempt < 1:
            raise ValueError("Executor attempt must be positive")
        existing = self.session.scalar(
            select(CostEntry).where(
                CostEntry.job_id == job_id,
                CostEntry.attempt == attempt,
            )
        )
        if existing is not None:
            return existing
        BudgetGuard().record_spend(project, cost)
        entry = CostEntry(
            project_id=project.id,
            job_id=job_id,
            attempt=attempt,
            role=role,
            provider=provider,
            model=model,
            input_tokens=0,
            output_tokens=0,
            est_cost=cost,
        )
        self.session.add(entry)
        self.session.flush()
        return entry


__all__ = ["CostEntry", "CostLedger"]
