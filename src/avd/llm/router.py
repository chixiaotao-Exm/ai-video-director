"""Role-based provider routing and one-retry structured response validation."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from avd.config import LLMRoute, Settings
from avd.state.models import Project

from .budget import BudgetGuard, acquire_project_budget
from .ledger import CostLedger
from .providers import ChatResponse, FakeProvider, Provider, ProviderConfigurationError
from .providers.anthropic import AnthropicProvider
from .providers.base import ProviderResponseError
from .providers.gemini import GeminiProvider
from .providers.openai import OpenAIProvider

Schema = TypeVar("Schema", bound=BaseModel)


def _strip_fences(text: str) -> str:
    """Accept JSON wrapped in a Markdown code fence, which chat models often add."""

    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


class StructuredOutputError(ValueError):
    """A provider returned invalid data both initially and after one repair."""


class LLMRouter:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        providers: Mapping[str, Provider] | None = None,
        ledger: CostLedger | None = None,
        project: Project | None = None,
        budget_guard: BudgetGuard | None = None,
        confirmation_check: Callable[[Project, float], bool] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.providers: dict[str, Provider] = dict(providers or {})
        self.ledger = ledger
        self.project = project
        self.budget_guard = budget_guard or BudgetGuard(self.settings.confirm_threshold)
        self.confirmation_check = confirmation_check

    def route(self, role: str) -> LLMRoute:
        route = self.settings.llm_route(role)
        if not route.model or not route.model.strip():
            setting = "AVD_GEMINI_MODEL" if route.provider == "gemini" else "AVD_LLM_ROUTES"
            raise ProviderConfigurationError(
                f"No model configured for role {role!r}; set {setting}"
            )
        return route

    def _provider(self, name: str) -> Provider:
        if name not in self.providers:
            types: dict[str, Callable[..., Provider]] = {
                "anthropic": AnthropicProvider,
                "openai": OpenAIProvider,
                "gemini": GeminiProvider,
            }
            if name == "deepseek":
                # DeepSeek exposes an OpenAI-compatible Chat Completions API.
                types["deepseek"] = partial(OpenAIProvider, label="DeepSeek")
            if name not in types:
                raise ProviderConfigurationError(f"Unknown LLM provider: {name!r}")
            key = getattr(self.settings, f"{name}_api_key")
            self.providers[name] = types[name](
                api_key=key.get_secret_value() if key else None,
                base_url=getattr(self.settings, f"{name}_base_url"),
                max_output_tokens=self.settings.llm_max_output_tokens,
            )
        return self.providers[name]

    def chat(
        self,
        role: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        project: Project | None = None,
        estimated_cost: float | None = None,
        confirmed: bool = False,
    ) -> ChatResponse:
        route = self.route(role)
        provider = self._provider(route.provider)
        current_project = project if project is not None else self.project
        self._preflight(
            route, provider, current_project, messages, tools, estimated_cost, confirmed
        )
        assert route.model is not None
        try:
            response = provider.chat(route.model, deepcopy(messages), deepcopy(tools))
        except ProviderResponseError as exc:
            self._record_response(current_project, role, route, exc.response)
            raise
        self._record_response(current_project, role, route, response)
        return response

    def _preflight(
        self,
        route: LLMRoute,
        provider: Provider,
        current_project: Project | None,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        estimated_cost: float | None,
        confirmed: bool,
    ) -> None:
        """Share accounting, reservations and human approval across model calls."""
        if self.ledger is not None and current_project is None:
            raise ProviderConfigurationError("A cost ledger requires a project for every call")
        if not isinstance(provider, FakeProvider) and (
            self.ledger is None or current_project is None
        ):
            raise ProviderConfigurationError("Live LLM calls require a project and CostLedger")
        assert route.model is not None
        if current_project is not None:
            acquire_project_budget(current_project)
            if estimated_cost is None and self.ledger is not None:
                # Conservative text planning estimate, not a token counter. Actual
                # usage is always recorded from the provider response afterward.
                estimated_input = math.ceil(len(json.dumps([messages, tools], default=str)) / 3)
                estimated_cost = self.ledger.estimate(
                    route.provider,
                    route.model,
                    estimated_input,
                    self.settings.llm_max_output_tokens,
                )
            if self.confirmation_check is not None:
                confirmed = self.confirmation_check(current_project, estimated_cost or 0.0)
            self.budget_guard.check(current_project, estimated_cost or 0.0, confirmed=confirmed)

    def _record_response(
        self, project: Project | None, role: str, route: LLMRoute, response: ChatResponse
    ) -> None:
        assert route.model is not None
        if self.ledger is not None and project is not None:
            self.ledger.record(
                project,
                role,
                route.provider,
                route.model,
                response.input_tokens,
                response.output_tokens,
            )

    def search(
        self,
        query: str,
        *,
        project: Project | None = None,
        estimated_cost: float | None = None,
        confirmed: bool = False,
    ) -> ChatResponse:
        """Run a cited web search through the configured search provider."""

        route = self.route("web_search")
        provider = self._provider(route.provider)
        search = getattr(provider, "search", None)
        if not callable(search):
            raise ProviderConfigurationError(
                f"Provider {route.provider!r} does not support web search"
            )
        current_project = project if project is not None else self.project
        self._preflight(
            route,
            provider,
            current_project,
            [{"role": "user", "content": query}],
            None,
            estimated_cost,
            confirmed,
        )
        assert route.model is not None
        try:
            response: ChatResponse = search(route.model, query)
        except ProviderResponseError as exc:
            self._record_response(current_project, "web_search", route, exc.response)
            raise
        self._record_response(current_project, "web_search", route, response)
        return response

    def structured(
        self,
        role: str,
        messages: list[dict[str, Any]],
        schema: type[Schema],
        *,
        project: Project | None = None,
        estimated_cost: float | None = None,
        confirmed: bool = False,
    ) -> Schema:
        request = deepcopy(messages)
        request.append(
            {
                "role": "user",
                "content": "Return only a JSON object matching this schema:\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False),
            }
        )
        for attempt in range(2):
            response = self.chat(
                role, request, project=project, estimated_cost=estimated_cost, confirmed=confirmed
            )
            try:
                return schema.model_validate_json(_strip_fences(response.content))
            except ValidationError as exc:
                if attempt == 1:
                    raise StructuredOutputError(
                        "Invalid structured response after one repair retry"
                    ) from exc
                request.append(response.as_message())
                request.append(
                    {
                        "role": "user",
                        "content": f"The JSON did not validate: {exc}. Repair it and return only valid JSON.",
                    }
                )
        raise AssertionError("unreachable")


__all__ = ["LLMRouter", "StructuredOutputError"]
