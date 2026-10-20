"""Opt-in, pinned model downloads used by setup.sh (PyYAML is installed there)."""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROFILES = frozenset({"draft12", "balanced24", "quality48"})


class HTTPSRedirects(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> Request | None:
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("Model redirects must use HTTPS without embedded credentials")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class ModelDownload:
    url: str
    destination: Path
    sha256: str | None


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def selected_models(document: object, root: Path, profile: str) -> list[ModelDownload]:
    """Validate the complete selection before creating files or contacting a server."""
    if profile not in PROFILES | {"none"}:
        raise ValueError("Unknown model profile")
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        raise ValueError("Manifest must contain a models list")
    if profile == "none":
        return []
    root = root.resolve()
    selected = []
    destinations: set[Path] = set()
    for entry in document["models"]:
        if not isinstance(entry, dict):
            raise ValueError("Each model entry must be a mapping")
        profiles = entry.get("profiles")
        if (
            not isinstance(profiles, list)
            or not profiles
            or any(not isinstance(value, str) or value not in PROFILES for value in profiles)
        ):
            raise ValueError("Each model must list explicit supported profiles")
        if profile not in profiles:
            continue
        item = validate_model(entry, root)
        if item.destination in destinations:
            raise ValueError("Multiple model entries target the same file")
        destinations.add(item.destination)
        selected.append(item)
    return selected


def validate_model(entry: dict[str, object], root: Path) -> ModelDownload:
    url = entry.get("url")
    if not isinstance(url, str):
        raise ValueError("Model URLs must be strings")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Model URLs must use HTTPS without embedded credentials")
    target = entry.get("target")
    if not isinstance(target, str) or not target or any(c in target for c in "\\:\x00\r\n"):
        raise ValueError("Model target must be a portable relative directory")
    target_path = Path(target)
    if target_path.is_absolute() or ".." in target_path.parts:
        raise ValueError("Model target must be relative to ComfyUI/models")
    directory = (root / target_path).resolve()
    filename = entry.get("filename") or unquote(Path(parsed.path).name)
    if (
        not isinstance(filename, str)
        or not filename
        or filename in {".", ".."}
        or any(character in filename for character in "/\\:\x00\r\n")
    ):
        raise ValueError("Model filename must be one non-empty portable path component")
    destination = directory / filename
    if not directory.is_relative_to(root) or destination.is_symlink():
        raise ValueError("Model destination must stay inside ComfyUI/models")
    expected = entry.get("sha256")
    if expected is not None and (
        not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected)
    ):
        raise ValueError("sha256 must contain exactly 64 hexadecimal characters")
    return ModelDownload(url, destination, expected.lower() if expected else None)


def download_models(document: object, root: Path, profile: str = "none") -> None:
    selected = selected_models(document, root, profile)
    if not selected:
        print(f"Model profile {profile}: no downloads selected")
        return
    opener = build_opener(HTTPSRedirects())
    for model in selected:
        destination = model.destination
        if destination.is_file() and destination.stat().st_size > 0:
            if model.sha256 is None or file_sha256(destination) == model.sha256:
                state = "verified" if model.sha256 else "not supplied"
                print(f"Keeping model: {destination} (hash {state})")
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = Request(model.url, headers={"User-Agent": "avd-gpu-setup/1"})
        if urlsplit(model.url).hostname == "huggingface.co" and os.environ.get("HF_TOKEN"):
            # Unredirected headers are dropped on redirects, including signed CDN URLs.
            request.add_unredirected_header("Authorization", "Bearer " + os.environ["HF_TOKEN"])
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent, prefix=".download-", delete=False
            ) as output:
                temporary = Path(output.name)
                with opener.open(request, timeout=120) as response:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
            if temporary.stat().st_size == 0:
                raise ValueError("Model download was empty")
            if model.sha256 and file_sha256(temporary) != model.sha256:
                raise ValueError("SHA-256 mismatch; previous model was preserved")
            os.replace(temporary, destination)
            print(f"Downloaded model: {destination}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> int:
    try:
        import yaml

        document = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
        profile = sys.argv[3] if len(sys.argv) > 3 else "none"
        download_models(document, Path(sys.argv[2]), profile)
    except Exception as exc:
        # Network exceptions may contain signed URLs; avoid printing those or any token.
        print(
            f"Model download failed ({type(exc).__name__}). "
            "Check manifest, profile, token, hash and disk space.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
