"""Role-based provider routing and one-retry structured response validation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import math
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from avd.config import LLMRoute, Settings
from avd.state.models import Project

from .ledger import CostLedger
from .budget import BudgetGuard
from .providers import ChatResponse, FakeProvider, Provider, ProviderConfigurationError
from .providers.anthropic import AnthropicProvider
from .providers.gemini import GeminiProvider
from .providers.openai import OpenAIProvider

Schema = TypeVar("Schema", bound=BaseModel)


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
        confirmed: bool = False,
    ) -> None:
        self.settings = settings or Settings()
        self.providers: dict[str, Provider] = dict(providers or {})
        self.ledger = ledger
        self.project = project
        self.budget_guard = budget_guard or BudgetGuard(self.settings.confirm_threshold)
        self.confirmed = confirmed

    def route(self, role: str) -> LLMRoute:
        route = self.settings.llm_route(role)
        if not route.model or not route.model.strip():
            setting = "AVD_GEMINI_MODEL" if route.provider == "gemini" else "AVD_LLM_ROUTES"
            raise ProviderConfigurationError(f"No model configured for role {role!r}; set {setting}")
        return route

    def _provider(self, name: str) -> Provider:
        if name not in self.providers:
            types = {"anthropic": AnthropicProvider, "openai": OpenAIProvider, "gemini": GeminiProvider}
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
        if self.ledger is not None and current_project is None:
            raise ProviderConfigurationError("A cost ledger requires a project for every call")
        if not isinstance(provider, FakeProvider) and (self.ledger is None or current_project is None):
            raise ProviderConfigurationError("Live LLM calls require a project and CostLedger")
        assert route.model is not None
        if current_project is not None:
            if estimated_cost is None and self.ledger is not None:
                # Conservative text planning estimate, not a token counter. Actual
                # usage is always recorded from the provider response afterward.
                estimated_input = math.ceil(len(json.dumps([messages, tools], default=str)) / 3)
                estimated_cost = self.ledger.estimate(
                    route.provider, route.model, estimated_input, self.settings.llm_max_output_tokens
                )
            self.budget_guard.check(current_project, estimated_cost or 0.0, confirmed=confirmed or self.confirmed)
        response = provider.chat(route.model, deepcopy(messages), deepcopy(tools))
        if self.ledger is not None and current_project is not None:
            self.ledger.record(
                current_project, role, route.provider, route.model,
                response.input_tokens, response.output_tokens,
            )
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
        request.append({
            "role": "user",
            "content": "Return only a JSON object matching this schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False),
        })
        for attempt in range(2):
            response = self.chat(
                role, request, project=project, estimated_cost=estimated_cost, confirmed=confirmed
            )
            try:
                return schema.model_validate_json(response.content)
            except ValidationError as exc:
                if attempt == 1:
                    raise StructuredOutputError("Invalid structured response after one repair retry") from exc
                request.append(response.as_message())
                request.append({
                    "role": "user",
                    "content": f"The JSON did not validate: {exc}. Repair it and return only valid JSON.",
                })
        raise AssertionError("unreachable")


__all__ = ["LLMRouter", "StructuredOutputError"]
