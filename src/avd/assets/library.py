"""Persistent character and voice asset library with consent gating."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from avd.state.consent import Asset, ConsentRequiredError, require_usable
from avd.state.models import Character, Voice

from .storage import AssetStorage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AssetLibrary:
    """Create, query, and consent-gate reusable project assets."""

    CHARACTER_VIEWS = frozenset({"front", "side", "half", "full"})

    def __init__(self, session: Session, storage: AssetStorage):
        self.session = session
        self.storage = storage

    def create_character(
        self,
        name: str,
        reference_images: Mapping[str, str],
        appearance_prompt: str,
        lora_path: str | None = None,
    ) -> Character:
        images = dict(reference_images)
        invalid_views = set(images) - self.CHARACTER_VIEWS
        if invalid_views:
            raise ValueError(
                f"Unsupported character reference views: {', '.join(sorted(invalid_views))}"
            )
        if not images.get("front"):
            raise ValueError("A front reference image is required")
        for path in images.values():
            self.storage.validate_source(path, "characters")

        character = Character(
            name=name,
            reference_images={},
            fixed_appearance_prompt=appearance_prompt,
            lora_path=lora_path,
        )
        self.session.add(character)
        self.session.flush()
        character.reference_images = {
            view: self.storage.store_character_image(character.id, path)
            for view, path in images.items()
        }
        self.session.flush()
        return character

    def create_voice(
        self,
        name: str,
        sample_paths: Sequence[str],
        engine: str,
        engine_voice_id: str | None = None,
        model_path: str | None = None,
        default_params: Mapping[str, object] | None = None,
    ) -> Voice:
        samples = list(sample_paths)
        if not samples:
            raise ValueError("At least one voice sample is required")
        for path in samples:
            self.storage.validate_source(path, "voices")

        voice = Voice(
            name=name,
            sample_audio_paths=[],
            engine=engine,
            engine_voice_id=engine_voice_id,
            model_path=model_path,
            default_params=dict(default_params or {}),
        )
        self.session.add(voice)
        self.session.flush()
        voice.sample_audio_paths = [
            self.storage.store_voice_sample(voice.id, path) for path in samples
        ]
        self.session.flush()
        return voice

    def confirm_consent(self, asset: Asset, confirmed_by: str) -> Asset:
        actor = confirmed_by.strip()
        if not actor:
            raise ValueError("confirmed_by is required")
        if not isinstance(asset, (Character, Voice)):
            raise TypeError("asset must be a Character or Voice")
        asset.consent_confirmed = True
        asset.consent_confirmed_by = actor
        asset.consent_confirmed_at = _utc_now()
        self.session.flush()
        return asset

    def get_character(self, character_id: str) -> Character | None:
        return self.session.get(Character, character_id)

    def get_voice(self, voice_id: str) -> Voice | None:
        return self.session.get(Voice, voice_id)

    def list_characters(self) -> list[Character]:
        return list(
            self.session.scalars(
                select(Character).order_by(Character.name, Character.id)
            ).all()
        )

    def list_voices(self) -> list[Voice]:
        return list(
            self.session.scalars(select(Voice).order_by(Voice.name, Voice.id)).all()
        )

    def update_character_prompt(
        self,
        character: Character | str,
        appearance_prompt: str,
    ) -> Character:
        resolved = (
            character
            if isinstance(character, Character)
            else self.get_character(character)
        )
        if resolved is None:
            raise LookupError(f"Character {character!r} was not found")
        resolved.fixed_appearance_prompt = appearance_prompt
        self.session.flush()
        return resolved

    @staticmethod
    def require_usable(asset: Asset) -> Asset:
        return require_usable(asset)


__all__ = ["AssetLibrary", "ConsentRequiredError", "require_usable"]
