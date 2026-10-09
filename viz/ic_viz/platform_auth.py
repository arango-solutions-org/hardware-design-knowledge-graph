"""Platform login: read the database as the user the Arango platform signed in.

Deployed on the Arango platform (Container Manager / BYOC), every request to
ChronoGraph passes the platform gateway, which forwards the signed-in user's
JWT as ``Authorization: bearer <jwt>``. The operator injects into the pod:

- ``ARANGO_DEPLOYMENT_ENDPOINT``, the in-cluster coordinator URL;
- ``ARANGO_DEPLOYMENT_CA``, the CA that signs that endpoint's certificate (a
  file path, or the PEM text);
- an integration sidecar (``INTEGRATION_HTTP_ADDRESS[_FULL]``) whose
  ``/_integration/authn/v1/identity`` names the user a token belongs to.

With these ChronoGraph reads the database as that user, with no stored
password: what each person can see is what their own database permissions
allow. The coordinator validates the JWT on every call.

Same module as project-sentinel's ``sentinel/platform_auth.py`` (adapted from
arango-cypher-py's, checked live on prod.demo). ChronoGraph only reads, inside
the request, so it needs no tokens minted for background work.

``CHRONO_PLATFORM_AUTH=off`` disables platform login (the service then uses
the baked account, as before).
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import threading
import time
from typing import Any

import requests

DEPLOYMENT_ENDPOINT_ENV = "ARANGO_DEPLOYMENT_ENDPOINT"
DEPLOYMENT_CA_ENV = "ARANGO_DEPLOYMENT_CA"
SIDECAR_ADDRESS_ENVS = ("INTEGRATION_HTTP_ADDRESS_FULL", "INTEGRATION_HTTP_ADDRESS")
PLATFORM_AUTH_ENV = "CHRONO_PLATFORM_AUTH"
#: A CA bundle (path) to verify the endpoint against instead of the injected one.
PLATFORM_CA_BUNDLE_ENV = "CHRONO_PLATFORM_CA_BUNDLE"
#: ``on`` / ``off`` override how the endpoint is verified.
PLATFORM_VERIFY_TLS_ENV = "CHRONO_PLATFORM_VERIFY_TLS"

_DISABLED_VALUES = frozenset({"off", "0", "false", "no"})
_ENABLED_VALUES = frozenset({"on", "1", "true", "yes"})
_TIMEOUT_S = 5.0

_logger = logging.getLogger(__name__)


def platform_endpoint() -> str | None:
    """The injected coordinator URL, or ``None`` off the platform (or when
    platform login is switched off). Never taken from the request."""
    if os.getenv(PLATFORM_AUTH_ENV, "auto").strip().lower() in _DISABLED_VALUES:
        return None
    value = os.getenv(DEPLOYMENT_ENDPOINT_ENV, "").strip().rstrip("/")
    return value or None


_ca_lock = threading.Lock()
_ca_files: dict[str, str] = {}


def deployment_ca() -> str | None:
    """A file holding the CA the platform injected for its endpoint, or ``None``.

    A PEM given as text is written once to a private temporary file, since TLS
    libraries take a path. ``None`` when unset, or when it names no file.
    """
    value = os.getenv(DEPLOYMENT_CA_ENV, "").strip()
    if not value:
        return None
    if "-----BEGIN" not in value:
        if os.path.isfile(value):
            return value
        _logger.warning("%s names no file (%s); the endpoint is not verified", DEPLOYMENT_CA_ENV, value)
        return None
    digest = hashlib.sha256(value.encode()).hexdigest()
    with _ca_lock:
        path = _ca_files.get(digest)
        if path is None or not os.path.isfile(path):
            fd, path = tempfile.mkstemp(prefix="arango-deployment-ca-", suffix=".pem")
            with os.fdopen(fd, "w") as f:
                f.write(value if value.endswith("\n") else value + "\n")
            _ca_files[digest] = path
        return path


def platform_tls_verify() -> bool | str:
    """How to verify the injected endpoint, as python-arango's
    ``verify_override`` takes it: a configured bundle, an explicit on/off, the
    injected CA, or no verification when no CA was injected."""
    bundle = os.getenv(PLATFORM_CA_BUNDLE_ENV, "").strip()
    if bundle:
        return bundle
    mode = os.getenv(PLATFORM_VERIFY_TLS_ENV, "auto").strip().lower()
    if mode in _ENABLED_VALUES:
        return True
    if mode in _DISABLED_VALUES:
        return False
    return deployment_ca() or False


def describe_tls_verify(verify: bool | str) -> str:
    if isinstance(verify, str):
        if verify == deployment_ca():
            return f"verified against the injected CA ({DEPLOYMENT_CA_ENV})"
        return f"verified against {PLATFORM_CA_BUNDLE_ENV}"
    return "verified against the system trust store" if verify else "not verified"


def forwarded_token(authorization: str | None) -> str | None:
    """The JWT in an ``Authorization: bearer <jwt>`` header, or ``None``."""
    value = authorization or ""
    if value[:7].lower() != "bearer ":
        return None
    return value[7:].strip() or None


def sidecar_address() -> str | None:
    for name in SIDECAR_ADDRESS_ENVS:
        value = os.getenv(name, "").strip().rstrip("/")
        if value:
            return value if "://" in value else f"http://{value}"
    return None


_identity_lock = threading.Lock()
_identities: dict[str, str] = {}
_MAX_IDENTITIES = 1000


def sidecar_identity(token: str) -> str | None:
    """The user *token* belongs to, as the sidecar (which validates it) says;
    ``None`` off the platform or when unknown. Cached by the token's hash."""
    address = sidecar_address()
    if address is None:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    with _identity_lock:
        if key in _identities:
            return _identities[key]
    try:
        resp = requests.get(
            f"{address}/_integration/authn/v1/identity",
            headers={"Authorization": f"bearer {token}"},
            timeout=_TIMEOUT_S,
        )
    except requests.exceptions.RequestException as exc:
        _logger.warning("integration sidecar identity lookup failed: %s", exc.__class__.__name__)
        return None
    if resp.status_code != 200:
        _logger.warning("integration sidecar identity lookup answered HTTP %s", resp.status_code)
        return None
    try:
        user = resp.json().get("user")
    except ValueError:
        return None
    if not isinstance(user, str) or not user:
        return None
    with _identity_lock:
        if len(_identities) >= _MAX_IDENTITIES:
            _identities.clear()
        _identities[key] = user
    return user


class PlatformTokenError(Exception):
    """The forwarded token is not a usable ArangoDB JWT. Never contains it."""


_client_lock = threading.Lock()
_clients: dict[tuple[str, str], Any] = {}


def open_platform_database(name: str, token: str) -> Any:
    """A database handle on the injected endpoint that authenticates every call
    with the user's JWT. Building it makes no request.

    python-arango decodes the token locally first, without the signature: it
    refuses an expired one, and one without an ``exp`` claim with a bare
    ``KeyError``. Those are narrowed to :class:`PlatformTokenError`.
    """
    import jwt
    from arango import ArangoClient
    from arango.exceptions import JWTExpiredError

    endpoint = platform_endpoint()
    if endpoint is None:
        raise PlatformTokenError("platform login is not configured here")
    verify = platform_tls_verify()
    key = (endpoint, str(verify))
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            client = _clients[key] = ArangoClient(hosts=endpoint, verify_override=verify)
    try:
        return client.db(name, auth_method="jwt", user_token=token)
    except JWTExpiredError as exc:
        raise PlatformTokenError("your platform login has expired") from exc
    except KeyError as exc:
        raise PlatformTokenError(f"your platform login has no {exc} claim") from exc
    except jwt.PyJWTError as exc:
        raise PlatformTokenError(f"your platform login is not a usable ArangoDB token ({exc})") from exc


def endpoint_answer(endpoint: str, token: str, verify: bool | str) -> str:
    """What one direct ``GET /_api/version`` with *token* gets: ``HTTP <code>``,
    or why it failed. Never includes the token."""
    try:
        resp = requests.get(
            f"{endpoint}/_api/version",
            headers={"Authorization": f"bearer {token}"},
            timeout=_TIMEOUT_S,
            verify=verify,
        )
    except requests.exceptions.SSLError as exc:
        return f"TLS verification failed ({exc.__class__.__name__})"
    except requests.exceptions.Timeout:
        return f"no answer within {_TIMEOUT_S:g}s"
    except requests.exceptions.ConnectionError as exc:
        return f"connection failed ({str(exc).replace(token, '<token>')[:200]})"
    except requests.exceptions.RequestException as exc:
        return f"request failed ({exc.__class__.__name__})"
    return f"HTTP {resp.status_code}"


def token_facts(token: str) -> dict[str, object]:
    """Algorithm, issuer, claim names and lifetime of a JWT, never a claim
    value. Unverified, so for diagnostics only."""
    import jwt

    try:
        header = jwt.get_unverified_header(token)
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        return {"parsable": False, "error": exc.__class__.__name__}
    exp, iat = payload.get("exp"), payload.get("iat")
    numeric = (int, float)
    return {
        "parsable": True,
        "alg": header.get("alg"),
        "iss": payload.get("iss"),
        "claims": sorted(payload),
        "lifetime_s": exp - iat if isinstance(exp, numeric) and isinstance(iat, numeric) else None,
        "expires_in_s": round(exp - time.time()) if isinstance(exp, numeric) else None,
    }
