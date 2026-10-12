"""
api.py — FastAPI app for ChronoGraph. Serves the JSON contract + the static SPA.

When deployed on the Arango platform (BYOC, see viz/DEPLOY.md) the service lives
under a mount prefix; set SERVICE_URL_PATH_PREFIX and ``asgi_app`` strips it
before routing (ic_viz/prefix.py). The front-end only uses relative URLs, so it
needs no build-time prefix. Run ``ic_viz.api:asgi_app`` — it is ``app`` itself
when no prefix is configured.
"""
from __future__ import annotations

import os
from typing import Optional

from arango.exceptions import ArangoServerError, JWTRefreshError
from fastapi import Depends, FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .datasource import get_source
from .platform import PlatformAccessDenied, PlatformLoginMiddleware, PlatformLoginRequired, current_login
from .prefix import StripServicePrefixMiddleware, configured_prefix

HERE = os.path.dirname(os.path.abspath(__file__))
VIZ_DIR = os.path.dirname(HERE)
WEB_DIR = os.path.join(VIZ_DIR, "web")


def create_app() -> FastAPI:
    app = FastAPI(title="ChronoGraph — IC Temporal Provenance Visualizer")
    # On the platform, read the database as the signed-in user (ic_viz/platform_auth.py).
    app.add_middleware(PlatformLoginMiddleware)
    src = get_source()
    app.state.src = src

    @app.exception_handler(PlatformLoginRequired)
    def _login_required(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=401)

    @app.exception_handler(PlatformAccessDenied)
    def _access_denied(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(JWTRefreshError)
    def _login_rejected(_request, _exc):
        # python-arango's answer to a 401 on a forwarded user token.
        return JSONResponse({"detail": "The database refused your platform login. Sign in again."},
                            status_code=401)

    @app.exception_handler(ArangoServerError)
    def _database_refused(_request, exc):
        # Access revoked since the last check (PlatformArangoSource rechecks
        # every few minutes): the database's own 401/403, not a server error.
        if exc.http_code in (401, 403):
            return JSONResponse({"detail": f"The database refused this request for your account "
                                           f"(HTTP {exc.http_code}, error {exc.error_code})."},
                                status_code=exc.http_code)
        return JSONResponse({"detail": f"database error (HTTP {exc.http_code}, error {exc.error_code})"},
                            status_code=500)

    @app.get("/api/platform/diagnostics")
    def platform_diagnostics():
        """What the platform provides this container, and whether each piece
        works: injected endpoint and CA, TLS policy and a direct request under
        it, the forwarded login, and whether the sidecar names the caller.
        Never a token or a claim value. In a browser, sign in to the platform
        at /ui/ first."""
        from . import platform_auth as pa

        endpoint = pa.platform_endpoint()
        verify = pa.platform_tls_verify()
        token = current_login()
        report = {
            "source": src.kind,
            "endpoint": {"injected": bool(os.getenv(pa.DEPLOYMENT_ENDPOINT_ENV, "").strip()), "in_use": endpoint is not None},
            "tls": {
                "ca_injected": bool(os.getenv(pa.DEPLOYMENT_CA_ENV, "").strip()),
                "ca_usable": pa.deployment_ca() is not None,
                "policy": pa.describe_tls_verify(verify),
            },
            "forwarded_login": pa.token_facts(token) if token else None,
            "sidecar": {"address_injected": pa.sidecar_address() is not None},
        }
        if endpoint and token:
            report["tls"]["direct_request"] = pa.endpoint_answer(endpoint, token, verify)
        if token and pa.sidecar_address():
            report["sidecar"]["identity_found"] = pa.sidecar_identity(token) is not None
        return report

    @app.get("/api/health")
    def health():
        return {"ok": True, "source": src.kind}

    @app.get("/healthz")
    def healthz():
        """Liveness + release proof for the platform deploy verifier: a 200 on
        ``/`` is served just as happily by the build being replaced, so the
        verifier compares this version to the release it uploaded."""
        return {"ok": True, "version": __version__, "source": src.kind}

    def readable():
        """On the platform, refuse a request whose user cannot read the
        database before anything is served: some routes answer from metadata
        cached by an earlier user's request without querying the database."""
        require_access = getattr(src, "require_access", None)
        if require_access is not None:
            require_access()

    reader = [Depends(readable)]

    @app.get("/api/repos", dependencies=reader)
    def repos():
        return src.repos()

    @app.get("/api/timeline", dependencies=reader)
    def timeline():
        return src.timeline()

    @app.get("/api/slice", dependencies=reader)
    def slice_(
        ts: int = Query(..., description="unix timestamp of the playhead"),
        repos: str = Query("or1200", description="comma-separated repo names"),
        projection: str = Query("traceability"),
    ):
        repo_list = [r for r in repos.split(",") if r]
        try:
            return src.slice(ts, repo_list, projection)
        except (PlatformLoginRequired, PlatformAccessDenied, JWTRefreshError):
            raise
        except ArangoServerError as e:
            if e.http_code in (401, 403):
                raise
            raise HTTPException(500, f"{e}")
        except Exception as e:
            raise HTTPException(500, f"slice failed: {e}")

    @app.get("/api/provenance", dependencies=reader)
    def provenance(id: str = Query(...)):
        try:
            return src.provenance(id)
        except NotImplementedError as e:
            raise HTTPException(501, str(e))
        except (PlatformLoginRequired, PlatformAccessDenied, JWTRefreshError):
            raise
        except ArangoServerError as e:
            if e.http_code in (401, 403):
                raise
            raise HTTPException(500, f"{e}")
        except Exception as e:
            raise HTTPException(500, f"provenance failed: {e}")

    @app.get("/api/source", dependencies=reader)
    def source(kind: str, ref: str, terms: Optional[str] = None):
        term_list = [t for t in (terms or "").split("|") if t]
        return src.source(kind, ref, term_list)

    @app.get("/api/search", dependencies=reader)
    def search(q: str, repos: Optional[str] = None):
        repo_list = [r for r in (repos or "").split(",") if r] or None
        return src.search(q, repo_list)

    # ---- static SPA ----
    if os.path.isdir(WEB_DIR):
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(os.path.join(WEB_DIR, "index.html"))

    return app


def with_prefix(inner, prefix: str | None = None):
    """``inner`` wrapped for a mount prefix (env SERVICE_URL_PATH_PREFIX by
    default). Returns ``inner`` unchanged when no prefix is configured."""
    prefix = configured_prefix() if prefix is None else prefix
    return StripServicePrefixMiddleware(inner, prefix) if prefix else inner


app = create_app()
asgi_app = with_prefix(app)
