"""Content-addressed storage for character images and voice samples."""

from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
from os import PathLike


class UnsupportedAssetError(ValueError):
    """Raised when a source file is missing or has an unsupported extension."""


class AssetStorage:
    """Copy user-provided media into the configured data directory."""

    IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
    AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".m4a"})

    def __init__(self, data_dir: str | PathLike[str]):
        self.data_dir = Path(data_dir).expanduser().resolve()

    def validate_source(
        self,
        source_path: str | PathLike[str],
        category: str,
    ) -> Path:
        """Validate a source and return its resolved path without copying it."""

        source = Path(source_path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"Asset source does not exist: {source}")
        suffix = source.suffix.lower()
        allowed = self._allowed_extensions(category)
        if suffix not in allowed:
            allowed_text = ", ".join(sorted(allowed))
            raise UnsupportedAssetError(
                f"Unsupported {category} asset extension {source.suffix!r}; "
                f"allowed extensions: {allowed_text}"
            )
        return source.resolve()

    def store(
        self,
        category: str,
        asset_id: str,
        source_path: str | PathLike[str],
    ) -> str:
        """Store one image/audio file and return its data-relative POSIX path."""

        source = self.validate_source(source_path, category)
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        category_name = self._category_name(category)
        suffix = source.suffix.lower()
        destination_dir = self.data_dir / "assets" / category_name / str(asset_id)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{digest}{suffix}"
        if not destination.exists():
            shutil.copyfile(source, destination)
        return destination.relative_to(self.data_dir).as_posix()

    def store_character_image(
        self,
        asset_id: str,
        source_path: str | PathLike[str],
    ) -> str:
        return self.store("characters", asset_id, source_path)

    def store_voice_sample(
        self,
        asset_id: str,
        source_path: str | PathLike[str],
    ) -> str:
        return self.store("voices", asset_id, source_path)

    @classmethod
    def _category_name(cls, category: str) -> str:
        normalized = category.strip().lower()
        if normalized in {"character", "characters", "image", "images"}:
            return "characters"
        if normalized in {"voice", "voices", "audio"}:
            return "voices"
        raise ValueError(f"Unknown asset category: {category!r}")

    @classmethod
    def _allowed_extensions(cls, category: str) -> frozenset[str]:
        return (
            cls.IMAGE_EXTENSIONS
            if cls._category_name(category) == "characters"
            else cls.AUDIO_EXTENSIONS
        )


__all__ = ["AssetStorage", "UnsupportedAssetError"]
