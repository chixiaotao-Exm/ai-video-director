"""Consent checks shared by project state and asset-library layers."""

from __future__ import annotations

from typing import TypeAlias

from .models import Character, Voice


class ConsentRequiredError(PermissionError):
    """Raised when an asset is used before its owner's consent is confirmed."""


Asset: TypeAlias = Character | Voice


def require_usable(asset: Asset) -> Asset:
    """Return an asset only when its consent gate has been confirmed."""

    if not isinstance(asset, (Character, Voice)):
        raise TypeError("asset must be a Character or Voice")
    if not asset.consent_confirmed:
        kind = "character" if isinstance(asset, Character) else "voice"
        raise ConsentRequiredError(
            f"Consent is required before using {kind} asset {asset.id}"
        )
    return asset


__all__ = ["Asset", "ConsentRequiredError", "require_usable"]
