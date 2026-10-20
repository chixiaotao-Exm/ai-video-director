"""Host and same-origin enforcement for the local, single-user web application."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_AUTHORITY = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _LOCAL_HOSTS
            or not _AUTHORITY.fullmatch(parsed.netloc)
            or "?" in value
            or "#" in value
            or any(char.isspace() for char in value)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        port = parsed.port
        return (
            parsed.scheme,
            parsed.hostname,
            port if port is not None else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class LocalOnlyMiddleware:
    """Protect every route, including static files, settings, and media."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        hosts = headers.get(b"host", [])
        own = None
        if len(hosts) == 1:
            own = _origin(f"{scope.get('scheme', 'http')}://{hosts[0].decode('latin-1')}")
        failure = None
        if own is None:
            failure = "Requests must use localhost or a loopback address"
        elif scope["method"] not in _SAFE_METHODS:
            origins = headers.get(b"origin", [])
            fetch_site = headers.get(b"sec-fetch-site", [])
            if any(value.lower() == b"cross-site" for value in fetch_site):
                failure = "Cross-site requests are not allowed"
            elif origins and (len(origins) != 1 or _origin(origins[0].decode("latin-1")) != own):
                failure = "Request Origin must match the application origin"
        if failure is not None:
            response = JSONResponse({"detail": failure}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


__all__ = ["LocalOnlyMiddleware"]
