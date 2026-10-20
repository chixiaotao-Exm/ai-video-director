"""Web search shares chat's budget, reservation, approval and usage boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from avd.config import LLMRoute, ModelPrice, Settings
from avd.director.review import approve_budget
from avd.llm import BudgetBusy, BudgetExceeded, ChatResponse, ConfirmationRequired, CostLedger
from avd.llm.providers import FakeProvider, ProviderConfigurationError
from avd.llm.providers.base import ProviderResponseError
from avd.llm.router import LLMRouter
from avd.runtime import Runtime
from avd.state.models import Project
from avd.state.repo import ProjectRepo


class FakeSearch(FakeProvider):
    def search(self, model: str, query: str) -> ChatResponse:
        return self.chat(model, [{"role": "user", "content": query}])


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[Runtime]:
    current = Runtime(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            confirm_threshold=None,
            llm_max_output_tokens=100,
            llm_routes={"web_search": LLMRoute(provider="openai", model="search-model")},
            model_prices={
                "openai/search-model": ModelPrice(input_per_million=1000, output_per_million=1000)
            },
        ),
        providers={
            "openai": FakeSearch(
                [ChatResponse(content="Cited result", input_tokens=4, output_tokens=6)]
            )
        },
    )
    yield current
    current.close()


def create_project(runtime: Runtime, budget: float | None) -> str:
    with runtime.session_factory() as session:
        project = ProjectRepo(session).create_project("Offline search", budget_limit=budget)
        session.commit()
        return project.id


def test_known_price_zero_budget_stops_search_before_provider(runtime: Runtime) -> None:
    identifier = create_project(runtime, 0)
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        with pytest.raises(BudgetExceeded, match="budget"):
            runtime.router(session, project).search("A real estimated query")
        assert project.spent == 0
        assert CostLedger(session).list(identifier) == []
    assert runtime.providers["openai"].calls == []


def test_query_and_output_tokens_both_contribute_to_search_estimate(runtime: Runtime) -> None:
    identifier = create_project(runtime, 0.2)
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        router = runtime.router(session, project)
        with pytest.raises(BudgetExceeded):
            router.search("a" * 3000)
        assert runtime.providers["openai"].calls == []
        assert router.search("short").content == "Cited result"
        assert project.spent == pytest.approx(0.01)


def test_search_obeys_queued_reservations(runtime: Runtime) -> None:
    identifier = create_project(runtime, 1)
    runtime.queue.submit("api", "image_gen", {"estimated_cost": 0.9}, project_id=identifier)
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        with pytest.raises(BudgetExceeded, match="Pending work"):
            runtime.router(session, project).search("Reserved work has priority")
    assert runtime.providers["openai"].calls == []


def test_search_checks_persistent_approval_even_when_caller_says_confirmed(
    runtime: Runtime,
) -> None:
    identifier = create_project(runtime, 10)
    runtime.settings.confirm_threshold = 0.01
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        router = runtime.router(session, project)
        with pytest.raises(ConfirmationRequired):
            router.search("Approval needed", estimated_cost=0.2, confirmed=True)
        assert runtime.providers["openai"].calls == []
        approve_budget(session, project, 0.25, "Offline human reviewer")
        assert router.search("Approved", estimated_cost=0.2).content == "Cited result"
        session.commit()
        entry = CostLedger(session).list(identifier)[0]
        assert (entry.role, entry.provider, entry.model) == ("web_search", "openai", "search-model")
        assert (entry.input_tokens, entry.output_tokens) == (4, 6)
        assert entry.est_cost == pytest.approx(0.01)


def test_reserved_work_consumes_the_human_approval_allowance(runtime: Runtime) -> None:
    identifier = create_project(runtime, 10)
    runtime.settings.confirm_threshold = 0.01
    runtime.queue.submit("api", "image_gen", {"estimated_cost": 0.9}, project_id=identifier)
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        approve_budget(session, project, 1, "Offline human reviewer")
        with pytest.raises(ConfirmationRequired):
            runtime.router(session, project).search(
                "Needs room in the approval", estimated_cost=0.2
            )
    assert runtime.providers["openai"].calls == []


def test_none_budget_remains_unlimited_despite_pending_work(runtime: Runtime) -> None:
    identifier = create_project(runtime, None)
    runtime.queue.submit("api", "image_gen", {"estimated_cost": 10000}, project_id=identifier)
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        assert runtime.router(session, project).search("Unlimited").content == "Cited result"
        session.commit()
        assert project.budget_limit is None
        assert project.spent == pytest.approx(0.01)


def test_search_holds_the_project_budget_lock_until_cost_commit(runtime: Runtime) -> None:
    identifier = create_project(runtime, 1)
    runtime.providers["openai"] = FakeSearch([ChatResponse(content="First", output_tokens=600)])
    with runtime.session_factory() as first, runtime.session_factory() as second:
        project = first.get(Project, identifier)
        stale = second.get(Project, identifier)
        runtime.router(first, project).search("First", estimated_cost=0.6)
        with pytest.raises(BudgetBusy):
            runtime.router(second, stale).search("Concurrent", estimated_cost=0.6)
        first.commit()
        with pytest.raises(BudgetExceeded):
            runtime.router(second, stale).search("After commit", estimated_cost=0.6)
        assert stale.spent == pytest.approx(0.6)
    assert len(runtime.providers["openai"].calls) == 1


def test_completed_unusable_search_response_is_still_accounted_once(runtime: Runtime) -> None:
    identifier = create_project(runtime, 10)
    runtime.providers["openai"] = FakeSearch(
        [
            ProviderResponseError(
                "Invalid normalized result", ChatResponse(input_tokens=50, output_tokens=100)
            )
        ]
    )
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        with pytest.raises(ProviderResponseError, match="normalized"):
            runtime.router(session, project).search("Malformed result")
        session.commit()
        entries = CostLedger(session).list(identifier)
        assert len(entries) == 1 and entries[0].role == "web_search"
        assert entries[0].est_cost == pytest.approx(0.15)
        assert project.spent == pytest.approx(0.15)
    assert len(runtime.providers["openai"].calls) == 1


def test_uncompleted_search_failure_does_not_invent_usage(runtime: Runtime) -> None:
    identifier = create_project(runtime, 10)
    runtime.providers["openai"] = FakeSearch([RuntimeError("Offline provider failed")])
    with runtime.session_factory() as session:
        project = session.get(Project, identifier)
        with pytest.raises(RuntimeError, match="Offline"):
            runtime.router(session, project).search("No completed usage")
        assert CostLedger(session).list(identifier) == []
        assert project.spent == 0


def test_fake_search_without_project_or_ledger_remains_available(runtime: Runtime) -> None:
    fake = FakeSearch(["Offline result"])
    router = LLMRouter(runtime.settings, providers={"openai": fake})
    assert router.search("No project test").content == "Offline result"


def test_search_ledger_requires_project_even_for_a_fake(runtime: Runtime) -> None:
    fake = FakeSearch(["Must not run"])
    with runtime.session_factory() as session:
        router = LLMRouter(runtime.settings, providers={"openai": fake}, ledger=CostLedger(session))
        with pytest.raises(ProviderConfigurationError, match="ledger requires a project"):
            router.search("Missing attribution")
    assert fake.calls == []


def test_live_search_cannot_bypass_accounting(runtime: Runtime) -> None:
    class UncalledLiveProvider:
        def chat(self, *args: Any, **kwargs: Any) -> ChatResponse:
            raise AssertionError("No SDK or network calls are allowed")

        def search(self, model: str, query: str) -> ChatResponse:
            raise AssertionError("No SDK or network calls are allowed")

    router = LLMRouter(runtime.settings, providers={"openai": UncalledLiveProvider()})
    with pytest.raises(ProviderConfigurationError, match="CostLedger"):
        router.search("No ledger or project")
