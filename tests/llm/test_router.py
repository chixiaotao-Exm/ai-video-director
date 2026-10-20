from __future__ import annotations

from collections.abc import Iterator

from pydantic import BaseModel
import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from avd.config import LLMRoute, ModelPrice, Settings
from avd.llm import BudgetExceeded, BudgetGuard, ChatResponse, ConfirmationRequired, CostLedger, FakeProvider, LLMRouter
from avd.llm.providers import ProviderConfigurationError
from avd.llm.providers.openai import OpenAIProvider
from avd.state.db import create_engine, create_session_factory, init_db
from avd.state.models import Project
from avd.state.repo import ProjectRepo


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:")
    init_db(engine)
    with create_session_factory(engine)() as current:
        yield current
    engine.dispose()


def test_default_role_routing_and_overrides() -> None:
    anthropic = FakeProvider(["director"])
    openai = FakeProvider(["script", "qa"])
    gemini = FakeProvider(["analysis"])
    router = LLMRouter(
        Settings(_env_file=None, gemini_model="configured-gemini"),
        providers={"anthropic": anthropic, "openai": openai, "gemini": gemini},
    )
    for role in ("director", "script", "qa", "analysis"):
        assert router.chat(role, [{"role": "user", "content": "hello"}]).content == role
    assert anthropic.calls[0]["model"] == "claude-opus-5-5"
    assert [call["model"] for call in openai.calls] == ["gpt-6.1-sol", "gpt-6.1-sol"]
    assert gemini.calls[0]["model"] == "configured-gemini"
    custom = Settings(_env_file=None, llm_routes={"script": LLMRoute(provider="anthropic", model="relay-model")})
    assert LLMRouter(custom).route("script").model == "relay-model"


def test_environment_configures_models_and_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AVD_SCRIPT_MODEL", "configured-script")
    monkeypatch.setenv("AVD_OPENAI_BASE_URL", "https://relay.invalid/v1")
    monkeypatch.setenv("AVD_GEMINI_MODEL", "configured-analysis")
    settings = Settings(_env_file=None)
    router = LLMRouter(settings)
    provider = router._provider("openai")
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://relay.invalid/v1"
    assert router.route("script").model == "configured-script"
    assert router.route("analysis").model == "configured-analysis"


def test_missing_analysis_model_fails_before_provider_call() -> None:
    fake = FakeProvider(["unused"])
    router = LLMRouter(Settings(_env_file=None, gemini_model=None), providers={"gemini": fake})
    with pytest.raises(ProviderConfigurationError, match="AVD_GEMINI_MODEL"):
        router.chat("analysis", [])
    assert fake.calls == []


class ResponseSchema(BaseModel):
    title: str
    count: int


def test_structured_repairs_once_without_mutating_messages(session: Session) -> None:
    project = ProjectRepo(session).create_project("structured", budget_limit=1)
    fake = FakeProvider([
        ChatResponse(content='{"title":"bad","count":"oops"}', input_tokens=100, output_tokens=20),
        ChatResponse(content='{"title":"fixed","count":2}', input_tokens=150, output_tokens=25),
    ])
    ledger = CostLedger(session, {"gpt-6.1-sol": ModelPrice(input_per_million=2, output_per_million=4)})
    router = LLMRouter(Settings(_env_file=None), providers={"openai": fake}, ledger=ledger, project=project)
    messages = [{"role": "user", "content": "give title and count"}]
    result = router.structured("script", messages, ResponseSchema)
    assert result == ResponseSchema(title="fixed", count=2)
    assert messages == [{"role": "user", "content": "give title and count"}]
    assert len(fake.calls) == 2
    assert "did not validate" in fake.calls[1]["messages"][-1]["content"]
    entries = ledger.list(project.id)
    assert len(entries) == 2
    assert sorted((entry.input_tokens, entry.output_tokens) for entry in entries) == [(100, 20), (150, 25)]
    assert project.spent == pytest.approx(0.00068)


def test_unknown_price_warns_and_registers_zero_cost(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    project = ProjectRepo(session).create_project("zero")
    ledger = CostLedger(session)
    entry = ledger.record(project, "script", "openai", "unknown", 100, 20)
    assert entry.est_cost == 0
    assert project.spent == 0
    assert "Unknown model price" in caplog.text
    assert "cost_entries" in inspect(session.get_bind()).get_table_names()


def test_ledger_persists_and_uses_provider_specific_price(session: Session) -> None:
    project = ProjectRepo(session).create_project("ledger")
    ledger = CostLedger(session, {
        "model": ModelPrice(input_per_million=100),
        "openai/model": ModelPrice(input_per_million=1, output_per_million=2),
    })
    ledger.record(project, "script", "openai", "model", 1_000_000, 500_000)
    session.commit()
    session.expire_all()
    assert project.spent == 2
    assert ledger.list(project.id)[0].est_cost == 2


def test_budget_boundary_confirmation_and_record_spend(session: Session) -> None:
    project = ProjectRepo(session).create_project("budget", budget_limit=10)
    guard = BudgetGuard(confirm_threshold=2)
    guard.check(project, 2)
    assert project.spent == 0
    with pytest.raises(ConfirmationRequired):
        guard.check(project, 3)
    guard.check(project, 10, confirmed=True)
    with pytest.raises(BudgetExceeded):
        guard.check(project, 10.01, confirmed=True)
    guard.record_spend(project, 2)
    assert project.spent == 2
    guard.check(project, 8, confirmed=True)
    with pytest.raises(BudgetExceeded):
        guard.check(project, 8.01, confirmed=True)


def test_none_is_unlimited_but_zero_is_zero_budget() -> None:
    guard = BudgetGuard(confirm_threshold=100)
    unlimited = Project(title="unlimited", budget_limit=None, spent=100)
    zero = Project(title="zero", budget_limit=0, spent=0)
    guard.check(unlimited, 100)
    guard.check(zero, 0)
    with pytest.raises(BudgetExceeded):
        guard.check(zero, 0.01)


@pytest.mark.parametrize("cost", [-1.0, float("inf"), float("nan")])
def test_budget_rejects_invalid_cost(cost: float) -> None:
    project = Project(title="invalid", budget_limit=None, spent=0)
    with pytest.raises(ValueError):
        BudgetGuard().check(project, cost)


def test_router_budget_denial_does_not_call_provider(session: Session) -> None:
    project = ProjectRepo(session).create_project("blocked", budget_limit=0)
    fake = FakeProvider(["unused"])
    router = LLMRouter(Settings(_env_file=None), providers={"openai": fake}, ledger=CostLedger(session), project=project)
    with pytest.raises(BudgetExceeded):
        router.chat("script", [], estimated_cost=0.1)
    assert fake.calls == []
    assert router.ledger is not None and router.ledger.list(project.id) == []


def test_repair_rechecks_budget_after_first_response(session: Session) -> None:
    project = ProjectRepo(session).create_project("repair-budget", budget_limit=0.1)
    fake = FakeProvider([ChatResponse(content="invalid", input_tokens=100_000), '{"title":"never","count":1}'])
    router = LLMRouter(
        Settings(_env_file=None), providers={"openai": fake}, project=project,
        ledger=CostLedger(session, {"gpt-6.1-sol": ModelPrice(input_per_million=1)}),
    )
    with pytest.raises(BudgetExceeded):
        router.structured("script", [], ResponseSchema, estimated_cost=0.01)
    assert len(fake.calls) == 1
    assert router.ledger is not None and len(router.ledger.list(project.id)) == 1
    assert project.spent == pytest.approx(0.1)


def test_live_provider_requires_accounting_before_any_sdk_call() -> None:
    router = LLMRouter(Settings(_env_file=None))
    with pytest.raises(ProviderConfigurationError, match="CostLedger"):
        router.chat("script", [])
