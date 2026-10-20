"""Application settings for local project state, storage, and backends."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


def _http_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() for char in value)
        )
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(
            "Service URLs must be HTTP(S) addresses without credentials, query or fragment"
        )
    return value


class ComfyUIProfile(BaseModel):
    """Connection details for a named local or remote ComfyUI server."""

    base_url: str = "http://127.0.0.1:8188"
    timeout: float = Field(default=600.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_base_url(self) -> Self:
        self.base_url = _http_base_url(self.base_url)
        return self


def _default_comfyui_profiles() -> dict[str, ComfyUIProfile]:
    return {
        "local": ComfyUIProfile(),
        "remote": ComfyUIProfile(),
    }


class LLMRoute(BaseModel):
    """A role's provider and model; a missing model is rejected at call time."""

    provider: str
    model: str | None = None


class ModelPrice(BaseModel):
    """Estimated US dollar price per one million input/output tokens."""

    input_per_million: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    output_per_million: float = Field(default=0.0, ge=0, allow_inf_nan=False)


class Settings(BaseSettings):
    """Configuration: explicit values > local settings file > ``AVD_`` variables."""

    _explicit_overrides: dict[str, Any] = PrivateAttr(default_factory=dict)

    data_dir: Path = Path("./data")
    database_url: str | None = None
    comfyui_profiles: dict[str, ComfyUIProfile] = Field(default_factory=_default_comfyui_profiles)
    comfyui_active_profile: str = "local"
    director_model: str = "claude-opus-5-5"
    script_model: str = "gpt-6-astra"
    qa_model: str = "gpt-6-astra"
    image_model: str = "gpt-image-2"
    # Gemini watches whole videos; OpenAI/Anthropic receive extracted keyframes.
    analysis_provider: Literal["openai", "anthropic", "gemini"] = "openai"
    analysis_model: str = "gpt-6-astra"
    gemini_model: str | None = "gemini-2.5-pro"
    subagent_provider: Literal["openai", "anthropic", "deepseek"] = "openai"
    subagent_model: str = "gpt-6-astra"
    deepseek_model: str = "deepseek-v4-pro"
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    openai_base_url: str | None = None
    gemini_base_url: str | None = None
    deepseek_base_url: str | None = "https://api.deepseek.com"
    web_search_enabled: bool = True
    web_search_provider: Literal["openai", "anthropic"] = "openai"
    web_search_model: str = "gpt-6-astra"
    ai_label: bool = False
    llm_routes: dict[str, LLMRoute] = Field(default_factory=dict)
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    confirm_threshold: float | None = Field(default=1.0, ge=0, allow_inf_nan=False)
    default_budget_limit: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    whisper_model: str = "small"
    workflow_dir: Path | None = None
    job_estimated_costs: dict[str, Annotated[float, Field(ge=0, allow_inf_nan=False)]] = Field(
        default_factory=dict
    )
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)
    log_format: Literal["text", "json"] = "text"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    job_timeout_seconds: float = Field(default=3600.0, gt=0, allow_inf_nan=False)
    job_timeouts: dict[str, Annotated[float, Field(gt=0, allow_inf_nan=False)]] = Field(
        default_factory=dict
    )
    # Jobs run in parallel per lane; one GPU usually holds one large model at a time.
    lane_concurrency: dict[str, Annotated[int, Field(ge=1, le=16)]] = Field(
        default_factory=lambda: {"gpu": 1, "api": 4, "cpu": 2}
    )
    worker_shutdown_grace_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    comfyui_url: str | None = None
    smoke_workflow: Path | None = None
    smoke_image_prompt: str | None = None
    smoke_image_reference: Path | None = None
    smoke_media_video: Path | None = None

    model_config = SettingsConfigDict(
        env_prefix="AVD_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
        hide_input_in_errors=True,
    )

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._explicit_overrides = deepcopy(values)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        from avd.settings_store import ReplacementSettingsSource, SettingsFileSource

        return (
            init_settings,
            ReplacementSettingsSource(
                SettingsFileSource(settings_cls, init_settings, env_settings, dotenv_settings)
            ),
            ReplacementSettingsSource(env_settings),
            ReplacementSettingsSource(dotenv_settings),
            ReplacementSettingsSource(file_secret_settings),
        )

    @property
    def explicit_overrides(self) -> dict[str, Any]:
        """Preserve constructor precedence when another process reloads the file."""
        return deepcopy(self._explicit_overrides)

    @field_validator(
        "anthropic_base_url",
        "openai_base_url",
        "gemini_base_url",
        "deepseek_base_url",
        "comfyui_url",
    )
    @classmethod
    def validate_service_url(cls, value: str | None) -> str | None:
        return _http_base_url(value) if value else None

    @field_validator("anthropic_base_url")
    @classmethod
    def strip_anthropic_version(cls, value: str | None) -> str | None:
        # The Anthropic SDK appends /v1 itself; relays are often entered with it.
        if value and value.endswith("/v1"):
            return value[: -len("/v1")]
        return value

    @model_validator(mode="after")
    def derive_database_url(self) -> Settings:
        if self.database_url is None:
            database_path = (self.data_dir / "state.db").expanduser().resolve()
            self.database_url = f"sqlite:///{database_path.as_posix()}"
        if self.comfyui_active_profile not in self.comfyui_profiles:
            raise ValueError(
                "Unknown active ComfyUI profile: "
                f"{self.comfyui_active_profile!r}; configured profiles: "
                f"{', '.join(sorted(self.comfyui_profiles))}"
            )
        return self

    @property
    def comfyui_profile(self) -> ComfyUIProfile:
        """The active ComfyUI connection, resolved by its configured name."""

        return self.comfyui_profiles[self.comfyui_active_profile]

    def llm_route(self, role: str) -> LLMRoute:
        defaults = {
            "director": LLMRoute(provider="anthropic", model=self.director_model),
            "script": LLMRoute(provider="openai", model=self.script_model),
            "qa": LLMRoute(provider="openai", model=self.qa_model),
            "analysis": LLMRoute(
                provider=self.analysis_provider,
                model=self.gemini_model
                if self.analysis_provider == "gemini"
                else self.analysis_model,
            ),
            "deepseek": LLMRoute(provider="deepseek", model=self.deepseek_model),
            "subagent": LLMRoute(provider=self.subagent_provider, model=self.subagent_model),
            "web_search": LLMRoute(
                provider=self.web_search_provider,
                model=self.web_search_model
                if self.web_search_provider == "openai"
                else self.director_model,
            ),
        }
        if role in self.llm_routes:
            return self.llm_routes[role]
        if role not in defaults:
            raise ValueError(f"Unknown LLM role: {role!r}")
        return defaults[role]


__all__ = ["ComfyUIProfile", "LLMRoute", "ModelPrice", "Settings"]
