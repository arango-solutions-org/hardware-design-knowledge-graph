"""The signed-in platform user, per request (see ``ic_viz/platform_auth.py``).

On the platform each request carries the user's JWT. This ASGI middleware
records it for the request, and :class:`ic_viz.datasource.PlatformArangoSource`
reads the database as that user. Off the platform nothing changes.

The token is kept in a ``ContextVar``, which Starlette copies into the thread
that runs each (synchronous) endpoint, so no endpoint signature changes.
"""
from __future__ import annotations

from contextvars import ContextVar

from starlette.types import ASGIApp, Receive, Scope, Send

from .platform_auth import forwarded_token, platform_endpoint

_login: ContextVar = ContextVar("chronograph_platform_login", default=None)


class PlatformLoginRequired(Exception):
    """A platform request arrived without a usable login (mapped to 401)."""


class PlatformAccessDenied(Exception):
    """The signed-in user cannot read the database (mapped to 403)."""


def on_platform() -> bool:
    return platform_endpoint() is not None


def current_login():
    return _login.get()


class PlatformLoginMiddleware:
    """Record the forwarded platform login for the duration of a request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not on_platform():
            await self.app(scope, receive, send)
            return
        authorization = None
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                authorization = value.decode("latin-1")
                break
        reset = _login.set(forwarded_token(authorization))
        try:
            await self.app(scope, receive, send)
        finally:
            _login.reset(reset)
