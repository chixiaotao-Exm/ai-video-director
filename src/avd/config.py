"""Application settings for local project state and asset storage."""

from __future__ import annotations

from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from constructor values or ``AVD_`` variables."""

    data_dir: Path = Path("./data")
    database_url: str | None = None

    model_config = SettingsConfigDict(
        env_prefix="AVD_",
        env_file=".env",
        extra="ignore",
    )

    @model_validator(mode="after")
    def derive_database_url(self) -> Settings:
        if self.database_url is None:
            database_path = (self.data_dir / "state.db").expanduser().resolve()
            self.database_url = f"sqlite:///{database_path.as_posix()}"
        return self


__all__ = ["Settings"]
