"""Atomic local configuration persistence, without exposing secret values to clients."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

if TYPE_CHECKING:
    from avd.config import Settings


SECRET_FIELDS = frozenset(
    {"anthropic_api_key", "openai_api_key", "gemini_api_key", "deepseek_api_key"}
)
READ_ONLY_FIELDS = frozenset({"data_dir", "database_url"})
_PREVIEW: ContextVar[tuple[Path, dict[str, Any]] | None] = ContextVar(
    "avd_settings_preview", default=None
)


class SettingsStoreError(ValueError):
    """A safe-to-display local configuration error; never includes submitted values."""


def _read_file(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.is_junction():
        raise SettingsStoreError("Local settings must be an ordinary file")
    if not path.exists():
        return {}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsStoreError("Cannot read local settings; check the settings file") from None
    if not isinstance(values, dict) or READ_ONLY_FIELDS.intersection(values):
        raise SettingsStoreError("Local settings contain invalid or read-only fields")
    return values


class SettingsFileSource(PydanticBaseSettingsSource):
    """Locate settings.json using higher-priority constructor or environment data_dir."""

    def __init__(
        self, settings_cls: type[BaseSettings], *location_sources: PydanticBaseSettingsSource
    ) -> None:
        super().__init__(settings_cls)
        self.location_sources = location_sources

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        directory: Any = "./data"
        for source in self.location_sources:
            values = source()
            if "data_dir" in values:
                directory = values["data_dir"]
                break
        path = Path(directory).expanduser().resolve() / "settings.json"
        preview = _PREVIEW.get()
        if preview is not None and preview[0] == path:
            return deepcopy(preview[1])
        return _read_file(path)


class ReplacementSettingsSource(PydanticBaseSettingsSource):
    """A higher-priority field replaces lower mappings, including an empty mapping."""

    def __init__(self, source: PydanticBaseSettingsSource) -> None:
        super().__init__(source.settings_cls)
        self.source = source

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        return {
            name: value for name, value in self.source().items() if name not in self.current_state
        }


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "••••")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {_redact(key, secrets): _redact(item, secrets) for key, item in value.items()}
    return value


def public_settings(settings: Settings) -> dict[str, Any]:
    """Return effective configuration with only a masked suffix for each API key."""
    raw_keys = [
        secret.get_secret_value()
        for name in SECRET_FIELDS
        if (secret := getattr(settings, name)) and secret.get_secret_value()
    ]
    values = {
        name: _redact(value, raw_keys)
        for name, value in settings.model_dump(mode="json", exclude=set(SECRET_FIELDS)).items()
    }
    secrets: dict[str, dict[str, Any]] = {}
    for name in sorted(SECRET_FIELDS):
        secret = getattr(settings, name)
        raw = secret.get_secret_value() if secret else ""
        # Very short keys must never be echoed in full, even as the suffix.
        suffix = raw[-4:] if len(raw) > 4 else ""
        secrets[name] = {
            "configured": bool(raw),
            "masked": _redact(f"••••{suffix}", raw_keys) if raw else "",
        }
    return {"settings": values, "secrets": secrets, "read_only": sorted(READ_ONLY_FIELDS)}


def _json_values(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_values(item) for item in value]
    return value


class SettingsStore:
    """A single server's editable overlay; workers reconstruct this store after spawning."""

    def __init__(self, settings: Settings) -> None:
        self.path = settings.data_dir.expanduser().resolve() / "settings.json"
        self.overrides = settings.explicit_overrides
        self.overrides.update(data_dir=settings.data_dir, database_url=settings.database_url)
        self._lock = threading.RLock()

    def load(self) -> Settings:
        from avd.config import Settings

        with self._lock:
            return Settings(**deepcopy(self.overrides))

    def update(self, changes: dict[str, Any]) -> Settings:
        from avd.config import Settings

        unknown = set(changes) - set(Settings.model_fields)
        if unknown or READ_ONLY_FIELDS.intersection(changes):
            raise SettingsStoreError("Unknown or read-only settings fields")
        values = deepcopy(changes)
        for name in SECRET_FIELDS.intersection(values):
            value = values[name]
            if value is not None and not isinstance(value, str):
                raise SettingsStoreError("API keys must be text or null")
            values[name] = value.strip() if value else None
            if not values[name]:
                values[name] = None
        with self._lock:
            current = self.load()
            try:
                Settings(**(current.model_dump() | values), _env_file=None)
            except (ValidationError, ValueError, TypeError):
                raise SettingsStoreError(
                    "Invalid settings values; check the selected fields"
                ) from None
            stored = _read_file(self.path)
            stored.update(values)
            # Validate the exact future source stack before replacing the file.
            # An explicit active-profile override can conflict with an otherwise
            # valid update of the profile dictionary, for example.
            token = _PREVIEW.set((self.path, stored))
            try:
                updated = self.load()
            except (ValidationError, ValueError, TypeError):
                raise SettingsStoreError(
                    "Settings conflict with explicit startup overrides"
                ) from None
            finally:
                _PREVIEW.reset(token)
            self._write(_json_values(stored))
            return updated

    def _write(self, values: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.is_junction():
            raise SettingsStoreError("Local settings must be an ordinary file")
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=".settings-", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(values, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, ValueError, TypeError):
            raise SettingsStoreError(
                "Could not save local settings; previous settings were kept"
            ) from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)


__all__ = ["SettingsStore", "SettingsStoreError", "public_settings"]
