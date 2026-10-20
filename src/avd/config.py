"""Application settings for local project state, storage, and backends."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ComfyUIProfile(BaseModel):
    """Connection details for a named local or remote ComfyUI server."""

    base_url: str = "http://127.0.0.1:8188"
    timeout: float = Field(default=600.0, gt=0)

    @model_validator(mode="after")
    def validate_base_url(self) -> Self:
        self.base_url = self.base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("ComfyUI profile base_url must use http:// or https://")
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
    """Configuration loaded from constructor values or ``AVD_`` variables."""

    data_dir: Path = Path("./data")
    database_url: str | None = None
    comfyui_profiles: dict[str, ComfyUIProfile] = Field(
        default_factory=_default_comfyui_profiles
    )
    comfyui_active_profile: str = "local"
    director_model: str = "claude-opus-5-5"
    script_model: str = "gpt-6.1-sol"
    qa_model: str = "gpt-6.1-sol"
    image_model: str = "gpt-image-2"
    gemini_model: str | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    openai_base_url: str | None = None
    gemini_base_url: str | None = None
    llm_routes: dict[str, LLMRoute] = Field(default_factory=dict)
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    confirm_threshold: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    whisper_model: str = "small"
    workflow_dir: Path | None = None
    job_estimated_costs: dict[str, float] = Field(default_factory=dict)
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, gt=0)

    model_config = SettingsConfigDict(
        env_prefix="AVD_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

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
            "analysis": LLMRoute(provider="gemini", model=self.gemini_model),
        }
        if role in self.llm_routes:
            return self.llm_routes[role]
        if role not in defaults:
            raise ValueError(f"Unknown LLM role: {role!r}")
        return defaults[role]


__all__ = ["ComfyUIProfile", "LLMRoute", "ModelPrice", "Settings"]
