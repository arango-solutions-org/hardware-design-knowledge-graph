"""Strip the platform mount prefix before routing (ChronoGraph on the Arango platform).

On the Arango platform (Container Manager / BYOC) a service is mounted at
``/_service/uds/_db/<db>/<instance>/`` and the ingress forwards requests with
that prefix intact, while the FastAPI routes stay mounted at ``/``, ``/api``
and ``/static``. This ASGI middleware removes the configured prefix from the
request path *the Starlette way*: ``scope["path"]`` keeps the full URL path and
the prefix is appended to ``scope["root_path"]``; Starlette's router then routes
on ``path`` minus ``root_path`` (``starlette._utils.get_route_path``), and
``Mount``/``StaticFiles`` extend ``root_path`` further as they descend. Shortening
``path`` directly (the older ASGI idiom) breaks ``StaticFiles`` on Starlette >= 0.33.
With no prefix configured it is a no-op, so the local ``python server.py`` path
is unchanged.

Ported from project-sentinel's ``sentinel/workspace/prefix.py`` (itself adapted
from arango-ontoextract's ``StripServicePrefixMiddleware``), verified live on
prod.demo.pilot.arango.ai.
"""

from __future__ import annotations

import os

from starlette.types import ASGIApp, Receive, Scope, Send

PREFIX_ENV = "SERVICE_URL_PATH_PREFIX"


def normalize_prefix(raw: str | None) -> str:
    """``" /a/b/ "`` -> ``"/a/b"``; empty/None -> ``""`` (meaning: no prefix)."""
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/")


def strip_prefix(path: str, prefix: str) -> str | None:
    """Path with ``prefix`` removed, or ``None`` when ``path`` is not under it.

    ``prefix`` must already be normalized. ``path == prefix`` maps to ``"/"``
    (the platform requires the trailing slash, but be lenient on input).
    """
    if not prefix:
        return None
    if path == prefix:
        return "/"
    if path.startswith(prefix + "/"):
        return path[len(prefix):]
    return None


def configured_prefix() -> str:
    """The prefix from the environment (empty when unset)."""
    return normalize_prefix(os.environ.get(PREFIX_ENV))


class StripServicePrefixMiddleware:
    """ASGI middleware: strip ``prefix`` from HTTP and WebSocket request paths."""

    def __init__(self, app: ASGIApp, prefix: str) -> None:
        self.app = app
        self.prefix = normalize_prefix(prefix)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not self.prefix:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if strip_prefix(path, self.prefix) is None:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        # Be lenient on the bare prefix (no trailing slash): route it as "/".
        scope["path"] = self.prefix + "/" if path == self.prefix else path
        scope["root_path"] = (scope.get("root_path") or "") + self.prefix
        await self.app(scope, receive, send)
