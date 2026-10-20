"""Project spending checks and post-call accounting."""

from __future__ import annotations

import math

from sqlalchemy import update
from sqlalchemy.orm import object_session

from avd.state.models import Project


class BudgetExceeded(RuntimeError):
    """The requested action would exceed the remaining project budget."""


class ConfirmationRequired(RuntimeError):
    """The action exceeds the configured threshold for user confirmation."""


def _validate_cost(value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError("Cost must be finite and non-negative")


class BudgetGuard:
    def __init__(self, confirm_threshold: float = 1.0) -> None:
        _validate_cost(confirm_threshold)
        self.confirm_threshold = confirm_threshold

    def check(
        self,
        project: Project,
        estimated_cost: float,
        *,
        confirmed: bool = False,
    ) -> None:
        """Validate without spending. Confirmation never overrides the project limit."""

        _validate_cost(estimated_cost)
        spent = project.spent or 0.0
        if project.budget_limit is not None and spent + estimated_cost > project.budget_limit:
            raise BudgetExceeded(
                f"Estimated cost ${estimated_cost:.6f} exceeds project remaining "
                f"budget ${project.budget_limit - spent:.6f}"
            )
        if estimated_cost > self.confirm_threshold and not confirmed:
            raise ConfirmationRequired(
                f"Estimated cost ${estimated_cost:.6f} exceeds confirmation "
                f"threshold ${self.confirm_threshold:.6f}"
            )

    def record_spend(self, project: Project, actual_cost: float) -> None:
        """Accumulate completed work; the owning transaction commits the change."""

        _validate_cost(actual_cost)
        session = object_session(project)
        if session is None:
            project.spent = (project.spent or 0.0) + actual_cost
            return
        session.flush()
        session.execute(
            update(Project)
            .where(Project.id == project.id)
            .values(spent=Project.spent + actual_cost)
            .execution_options(synchronize_session="fetch")
        )


__all__ = ["BudgetExceeded", "BudgetGuard", "ConfirmationRequired"]
