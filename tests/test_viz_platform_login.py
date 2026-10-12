"""
ChronoGraph platform login (viz/ic_viz/platform_auth.py, platform.py,
datasource.PlatformArangoSource, the deploy entrypoint).

On the Arango platform the gateway forwards each signed-in user's JWT, and
ChronoGraph reads the database as that user, so the bundle carries no account.
One real HTTP server on localhost plays the coordinator (``/_api/version``,
``/_db/<db>/_api/database/current`` and the AQL cursor) and the integration
sidecar (``/_integration/authn/v1/identity``); database handles are real
python-arango objects.
"""
from __future__ import annotations

import base64
import importlib.machinery
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VIZ_DIR = REPO_ROOT / "viz"
sys.path.insert(0, str(VIZ_DIR))

from ic_viz import platform_auth  # noqa: E402

DATABASE = "ic-test"
PLATFORM_ENV = (
    "ARANGO_DEPLOYMENT_ENDPOINT",
    "ARANGO_DEPLOYMENT_CA",
    "INTEGRATION_HTTP_ADDRESS_FULL",
    "INTEGRATION_HTTP_ADDRESS",
    "CHRONO_PLATFORM_AUTH",
    "CHRONO_PLATFORM_CA_BUNDLE",
    "CHRONO_PLATFORM_VERIFY_TLS",
    "ARANGO_DATABASE",
)


def _jwt(**claims) -> str:
    def enc(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc(claims)}.c2ln"


def _login(user: str, lifetime_s: int = 3600) -> str:
    now = int(time.time())
    return _jwt(iss="arangodb", preferred_username=user, iat=now, exp=now + lifetime_s)


ALICE = _login("alice")  # ro on the database
BOB = _login("bob")  # signed in, no access to the database
EXPIRED = _login("alice", lifetime_s=-60)
NO_EXP = _jwt(iss="arangodb", preferred_username="alice")


class FakePlatform:
    """Coordinator + sidecar. ``users`` maps an accepted token to its user;
    ``readers`` are the users with access to DATABASE."""

    def __init__(self):
        self.users = {ALICE: "alice", BOB: "bob"}
        self.readers = {"alice"}
        self.queries: list[str] = []  # the user each AQL cursor ran as
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _caller(self):
                auth = self.headers.get("Authorization", "")
                token = auth[7:] if auth.lower().startswith("bearer ") else None
                return fake.users.get(token)

            def _refuse(self, status):
                self._send(status, {"error": True, "code": status, "errorNum": 11,
                                    "errorMessage": "not authorized"})

            def do_GET(self):
                user = self._caller()
                if self.path == "/_integration/authn/v1/identity":
                    self._send(200, {"user": user}) if user else self._send(401, {})
                elif self.path == "/_api/version":
                    self._send(200, {"server": "arango", "version": "3.12"}) if user else self._refuse(401)
                elif self.path == f"/_db/{DATABASE}/_api/database/current":
                    if user is None:
                        self._refuse(401)
                    elif user not in fake.readers:
                        self._refuse(403)
                    else:
                        self._send(200, {"error": False, "code": 200,
                                         "result": {"name": DATABASE, "id": "1", "path": "",
                                                    "isSystem": False}})
                else:
                    self._send(404, {})

            def do_POST(self):
                user = self._caller()
                if self.path != f"/_db/{DATABASE}/_api/cursor":
                    self._send(404, {})
                    return
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if user is None:
                    self._refuse(401)
                    return
                if user not in fake.readers:
                    self._refuse(403)
                    return
                fake.queries.append(user)
                self._send(201, {"error": False, "code": 201, "result": [], "hasMore": False,
                                 "cached": False, "extra": {}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in PLATFORM_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(platform_auth, "_identities", {})
    monkeypatch.setattr(platform_auth, "_clients", {})


@pytest.fixture
def fake(monkeypatch):
    platform = FakePlatform()
    monkeypatch.setenv("ARANGO_DEPLOYMENT_ENDPOINT", platform.address)
    monkeypatch.setenv("INTEGRATION_HTTP_ADDRESS_FULL", platform.address)
    monkeypatch.setenv("ARANGO_DATABASE", DATABASE)
    yield platform
    platform.close()


@pytest.fixture
def client(fake):
    from fastapi.testclient import TestClient

    from ic_viz import api  # on the platform, importing it connects nowhere

    return TestClient(api.create_app())


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"bearer {token}"}


class TestSource:
    def test_on_the_platform_the_source_reads_as_the_signed_in_user(self, fake):
        from ic_viz.datasource import PlatformArangoSource, get_source

        source = get_source()
        assert isinstance(source, PlatformArangoSource) and source.database == DATABASE

    def test_on_the_platform_the_database_must_be_named(self, fake, monkeypatch):
        from ic_viz.datasource import get_source

        monkeypatch.delenv("ARANGO_DATABASE")
        with pytest.raises(RuntimeError, match="ARANGO_DATABASE"):
            get_source()

    def test_platform_login_can_be_switched_off(self, fake, monkeypatch):
        from ic_viz.platform import on_platform

        assert on_platform()
        monkeypatch.setenv("CHRONO_PLATFORM_AUTH", "off")
        assert not on_platform()

    def test_off_the_platform_nothing_changes(self):
        from ic_viz.platform import on_platform

        assert not on_platform()


class TestRequests:
    def test_a_reader_is_served_and_the_query_runs_as_them(self, client, fake):
        response = client.get("/api/repos", headers=_as(ALICE))
        assert response.status_code == 200, response.text
        assert response.json()["meta"]["source"] == "arango"
        assert fake.queries and set(fake.queries) == {"alice"}

    def test_without_a_login_the_answer_is_401(self, client, fake):
        response = client.get("/api/repos")
        assert response.status_code == 401
        assert "platform login" in response.json()["detail"]
        assert fake.queries == []

    @pytest.mark.parametrize("token", [EXPIRED, NO_EXP], ids=["expired", "no-exp-claim"])
    def test_an_unusable_login_is_401_not_500(self, client, fake, token):
        response = client.get("/api/repos", headers=_as(token))
        assert response.status_code == 401
        assert token not in response.text

    def test_a_login_the_database_rejects_is_401(self, client, fake):
        response = client.get("/api/repos", headers=_as(_login("mallory")))
        assert response.status_code == 401

    def test_a_user_without_access_is_refused_even_from_the_cache(self, client, fake):
        assert client.get("/api/repos", headers=_as(ALICE)).status_code == 200
        before = list(fake.queries)
        # /api/timeline answers from epochs cached by alice's request
        for path in ("/api/timeline", "/api/repos", "/api/search?q=x"):
            response = client.get(path, headers=_as(BOB))
            assert response.status_code == 403, path
            assert DATABASE in response.json()["detail"]
        assert fake.queries == before

    def test_a_login_error_inside_a_route_is_not_turned_into_500(self, client, fake):
        response = client.get("/api/slice?ts=1&repos=or1200", headers=_as(BOB))
        assert response.status_code == 403

    def test_a_database_refusal_inside_a_catch_all_route_keeps_its_status(self, client, fake):
        assert client.get("/api/repos", headers=_as(ALICE)).status_code == 200
        fake.readers.discard("alice")  # revoked while the access check is still fresh
        response = client.get("/api/slice?ts=1&repos=or1200", headers=_as(ALICE))
        assert response.status_code == 403, response.text

    def test_access_is_rechecked_after_the_ttl(self, client, fake, monkeypatch):
        from ic_viz.datasource import PlatformArangoSource

        assert client.get("/api/repos", headers=_as(ALICE)).status_code == 200
        fake.readers.discard("alice")
        # within the TTL the database itself refuses the query: 403, not 500
        within = client.get("/api/repos", headers=_as(ALICE))
        assert within.status_code == 403 and "refused this request" in within.json()["detail"]
        monkeypatch.setattr(PlatformArangoSource, "_ACCESS_TTL_S", -1.0)
        after = client.get("/api/timeline", headers=_as(ALICE))
        assert after.status_code == 403 and "cannot read" in after.json()["detail"]

    def test_health_and_the_page_need_no_database_access(self, client):
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/health").json()["source"] == "arango"


class TestDiagnostics:
    def test_reports_what_the_platform_provides_without_secrets(self, client):
        response = client.get("/api/platform/diagnostics", headers=_as(ALICE))
        assert response.status_code == 200
        body = response.json()
        assert body["endpoint"] == {"injected": True, "in_use": True}
        assert body["tls"]["direct_request"] == "HTTP 200"
        assert body["sidecar"] == {"address_injected": True, "identity_found": True}
        assert "preferred_username" in body["forwarded_login"]["claims"]
        assert ALICE not in response.text and "alice" not in response.text

    def test_without_a_login_it_still_answers(self, client):
        body = client.get("/api/platform/diagnostics").json()
        assert body["forwarded_login"] is None and "direct_request" not in body["tls"]


def _load_entrypoint():
    path = VIZ_DIR / "deploy" / "entrypoint"
    loader = importlib.machinery.SourceFileLoader("chronograph_entrypoint", str(path))
    spec = importlib.util.spec_from_loader("chronograph_entrypoint", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestEntrypoint:
    entry = _load_entrypoint()

    def test_platform_login_needs_no_password(self):
        env = {"ARANGO_DEPLOYMENT_ENDPOINT": "https://c.svc:8529"}
        assert self.entry._platform_login(env)
        assert self.entry._login_problem(env) is None

    def test_a_credentialed_bundle_turns_platform_login_off(self):
        env = {"ARANGO_DEPLOYMENT_ENDPOINT": "https://c.svc:8529", "CHRONO_PLATFORM_AUTH": "off",
               "ARANGO_ENDPOINT": "https://c.example", "ARANGO_PASSWORD": "pw"}
        assert not self.entry._platform_login(env)
        assert self.entry._login_problem(env) is None

    def test_no_login_at_all_is_refused_with_the_fix(self):
        for env in ({}, {"ARANGO_DEPLOYMENT_ENDPOINT": "https://c.svc:8529",
                         "CHRONO_PLATFORM_AUTH": "off"}):
            problem = self.entry._login_problem(env)
            assert problem and "--with-credentials" in problem
