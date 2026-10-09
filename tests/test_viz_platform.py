"""
Guards for deploying ChronoGraph (viz/) on the Arango platform (BYOC).

See viz/DEPLOY.md. Three properties break silently on the platform — a blank
page with a green light — so they are pinned here instead of discovered live:

1. Prefix routing: the ingress forwards ``/_service/uds/_db/<db>/<instance>/...``
   intact; StripServicePrefixMiddleware must make API routes AND StaticFiles
   resolve under it, while staying a no-op locally.
2. Relative URLs: no root-absolute ``/static`` or ``/api`` reference in the
   front-end (they miss the mount prefix and 404).
3. The bundle: flat layout, entrypoint token rule, sanitized .env (by default
   no account at all — each signed-in platform user reads as themselves; with
   --with-credentials a service account, never root by default; never an API
   key), prefix == mount path — and byoc_deploy's pre-flight rejects each way
   of getting that wrong.
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VIZ_DIR = REPO_ROOT / "viz"
WEB_DIR = VIZ_DIR / "web"
PACKAGE_SH = VIZ_DIR / "deploy" / "package.sh"
PREFIX = "/_service/uds/_db/testdb/chronograph"

sys.path.insert(0, str(VIZ_DIR))
from ic_viz.prefix import StripServicePrefixMiddleware, normalize_prefix, strip_prefix  # noqa: E402


def _load_byoc_deploy():
    spec = importlib.util.spec_from_file_location("byoc_deploy", VIZ_DIR / "deploy" / "byoc_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


byoc = _load_byoc_deploy()


# ── 1. prefix routing ────────────────────────────────────────────────────────
class TestPrefixHelpers:
    def test_normalize(self):
        assert normalize_prefix(" /a/b/ ") == "/a/b"
        assert normalize_prefix("a/b") == "/a/b"
        assert normalize_prefix("") == ""
        assert normalize_prefix(None) == ""

    def test_strip(self):
        assert strip_prefix(PREFIX + "/api/repos", PREFIX) == "/api/repos"
        assert strip_prefix(PREFIX, PREFIX) == "/"
        assert strip_prefix(PREFIX + "/", PREFIX) == "/"
        assert strip_prefix("/api/repos", PREFIX) is None
        # a sibling instance sharing a name prefix must not match
        assert strip_prefix(PREFIX + "-v2/api", PREFIX) is None
        assert strip_prefix("/anything", "") is None


@pytest.fixture()
def mini_app(tmp_path):
    """Same shape as ic_viz.api (JSON route + "/" index + StaticFiles at /static),
    without connecting to a database at import time."""
    from fastapi import FastAPI
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "app.js").write_text("console.log('ok');")
    (tmp_path / "index.html").write_text('<script src="./static/js/app.js"></script>')

    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(tmp_path / "index.html")

    app.mount("/static", StaticFiles(directory=tmp_path), name="static")
    return app


class TestPrefixMiddleware:
    def test_routes_and_static_under_prefix(self, mini_app):
        from fastapi.testclient import TestClient

        client = TestClient(StripServicePrefixMiddleware(mini_app, PREFIX))
        assert client.get(PREFIX + "/healthz").json() == {"ok": True}
        index = client.get(PREFIX + "/")
        assert index.status_code == 200 and "./static/js/app.js" in index.text
        # StaticFiles is the part that breaks with naive path-shortening
        asset = client.get(PREFIX + "/static/js/app.js")
        assert asset.status_code == 200 and "console.log" in asset.text
        # bare prefix (no trailing slash) is routed as "/"
        assert client.get(PREFIX).status_code == 200

    def test_unprefixed_paths_still_work(self, mini_app):
        """No-op locally: unprefixed requests pass straight through."""
        from fastapi.testclient import TestClient

        client = TestClient(StripServicePrefixMiddleware(mini_app, PREFIX))
        assert client.get("/healthz").status_code == 200
        assert client.get("/static/js/app.js").status_code == 200

    def test_empty_prefix_is_noop(self, mini_app):
        from fastapi.testclient import TestClient

        client = TestClient(StripServicePrefixMiddleware(mini_app, ""))
        assert client.get("/healthz").status_code == 200
        assert client.get(PREFIX + "/healthz").status_code == 404


# ── 2. relative URLs ─────────────────────────────────────────────────────────
def _frontend_files():
    for path in sorted(WEB_DIR.rglob("*")):
        if path.is_file() and "vendor" not in path.parts and path.suffix in {".html", ".js", ".css"}:
            yield path


@pytest.mark.parametrize("path", list(_frontend_files()), ids=lambda p: str(p.relative_to(VIZ_DIR)))
def test_frontend_has_no_root_absolute_urls(path):
    text = path.read_text(encoding="utf-8")
    hits = re.findall(r"""(?:href|src)=["']/(?:static|api)|['"`(]/(?:static|api)/""", text)
    assert not hits, f"{path.name}: root-absolute URL(s) {hits} break under the platform mount prefix"


def test_index_references_relative_assets():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="(\./static/[^"]+)"', html)
    assert len(assets) >= 5, "index.html should reference its assets as ./static/..."
    for asset in assets:
        assert (WEB_DIR / asset[len("./static/"):]).is_file(), f"{asset} does not exist under viz/web"


# ── 3. bundle + pre-flight ───────────────────────────────────────────────────
def _run_package(out: Path, extra_env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "ENV_FILE": "/nonexistent/.env",  # hermetic: never read the developer's .env
        "ARANGO_ENDPOINT": "https://cluster.example.invalid",
        "ARANGO_DATABASE": "testdb",
        **extra_env,
    }
    return subprocess.run(["bash", str(PACKAGE_SH), str(out), *args],
                          env=env, capture_output=True, text=True)


SERVICE_ENV = {"SERVICE_ARANGO_USERNAME": "chrono-svc", "SERVICE_ARANGO_PASSWORD": "s3cret"}


def _members(tarball: Path) -> dict[str, str]:
    with tarfile.open(tarball, "r:gz") as archive:
        return {byoc.tar_member_name(n): n for n in archive.getnames()}


def _read(tarball: Path, member: str) -> str:
    with tarfile.open(tarball, "r:gz") as archive:
        raw = _members(tarball)[member]
        return archive.extractfile(raw).read().decode()


def _baked_env(tarball: Path) -> dict[str, str]:
    return dict(l.split("=", 1) for l in _read(tarball, ".env").splitlines()
                if "=" in l and not l.startswith("#"))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    """The default bundle: platform login, no account baked."""
    out = tmp_path_factory.mktemp("bundle") / "chronograph-service.tar.gz"
    result = _run_package(out, SERVICE_ENV)
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture(scope="module")
def credentialed_bundle(tmp_path_factory):
    """A --with-credentials bundle: a service account baked, platform login off."""
    out = tmp_path_factory.mktemp("bundle") / "chronograph-credentials.tar.gz"
    result = _run_package(out, SERVICE_ENV, "--with-credentials")
    assert result.returncode == 0, result.stderr
    return out


class TestPackage:
    def test_flat_layout(self, bundle):
        names = _members(bundle)
        for required in byoc.REQUIRED_MEMBERS:
            assert required in names, f"{required} missing"
        assert ".env" in names
        assert not any("__pycache__" in n for n in names)
        assert "viz/snapshot/snapshot.json" not in names, "the 6MB offline snapshot must not ship"

    def test_entrypoint_token_rule(self, bundle):
        assert _read(bundle, "entrypoint").splitlines()[0].startswith("entrypoint")

    def test_default_bundle_bakes_no_account(self, bundle):
        env = _baked_env(bundle)
        assert set(env) == {"ARANGO_ENDPOINT", "ARANGO_DATABASE", "SERVICE_URL_PATH_PREFIX",
                            "CHRONO_SOURCE"}
        assert env["SERVICE_URL_PATH_PREFIX"] == PREFIX
        assert env["CHRONO_SOURCE"] == "arango"
        assert "s3cret" not in _read(bundle, ".env")

    def test_default_bundle_needs_no_credentials_to_build(self, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = _run_package(out, {})
        assert result.returncode == 0, result.stderr
        assert "no credentials" in result.stdout

    def test_credentialed_bundle_bakes_the_service_account(self, credentialed_bundle):
        env = _baked_env(credentialed_bundle)
        assert set(env) == {"ARANGO_ENDPOINT", "ARANGO_USERNAME", "ARANGO_PASSWORD",
                            "CHRONO_PLATFORM_AUTH", "ARANGO_DATABASE",
                            "SERVICE_URL_PATH_PREFIX", "CHRONO_SOURCE"}
        assert env["ARANGO_USERNAME"] == "chrono-svc"
        assert env["CHRONO_PLATFORM_AUTH"] == "off"
        assert env["SERVICE_URL_PATH_PREFIX"] == PREFIX

    def test_credentialed_bundle_needs_credentials(self, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = _run_package(out, {}, "--with-credentials")
        assert result.returncode != 0 and "--with-credentials needs" in result.stderr
        assert not out.exists()

    def test_api_keys_never_baked(self, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = _run_package(out, {**SERVICE_ENV, "OPENAI_API_KEY": "sk-should-not-ship"})
        assert result.returncode == 0, result.stderr
        assert "sk-should-not-ship" not in _read(out, ".env")

    def test_pyproject_deps_match_requirements(self, bundle):
        pyproject = _read(bundle, "pyproject.toml")
        reqs = [l.strip() for l in (VIZ_DIR / "requirements.txt").read_text().splitlines()
                if l.strip() and not l.startswith("#")]
        for req in reqs:
            assert f'"{req}"' in pyproject
        assert re.search(r'version = "\d+\.\d+\.\d+"', pyproject)

    def test_refuses_root_without_flag(self, tmp_path):
        out = tmp_path / "b.tar.gz"
        root = {"ARANGO_USERNAME": "root", "ARANGO_PASSWORD": "x"}
        result = _run_package(out, root, "--with-credentials")
        assert result.returncode != 0 and "root" in result.stderr
        assert not out.exists()
        assert _run_package(out, root, "--with-credentials", "--allow-root").returncode == 0

    def test_default_bundle_ignores_a_root_account_in_the_environment(self, tmp_path):
        out = tmp_path / "b.tar.gz"
        result = _run_package(out, {"ARANGO_USERNAME": "root", "ARANGO_PASSWORD": "x"})
        assert result.returncode == 0, result.stderr
        assert "ARANGO_USERNAME" not in _baked_env(out)

    def test_refuses_loopback_endpoint(self, tmp_path):
        result = _run_package(tmp_path / "b.tar.gz",
                              {**SERVICE_ENV, "ARANGO_ENDPOINT": "http://localhost:8529"})
        assert result.returncode != 0 and "loopback" in result.stderr


def _rewrite(src: Path, dst: Path, replace: dict[str, str]) -> Path:
    """Copy a bundle, substituting whole member contents."""
    with tarfile.open(src, "r:gz") as old, tarfile.open(dst, "w:gz") as new:
        for member in old.getmembers():
            name = byoc.tar_member_name(member.name)
            if name in replace and member.isfile():
                data = replace[name].encode()
                member.size = len(data)
                new.addfile(member, io.BytesIO(data))
            else:
                new.addfile(member, old.extractfile(member) if member.isfile() else None)
    return dst


class TestPreflight:
    def test_good_bundle_passes(self, bundle):
        byoc.preflight(bundle, "chronograph", "testdb")

    def test_good_credentialed_bundle_passes(self, credentialed_bundle):
        byoc.preflight(credentialed_bundle, "chronograph", "testdb")

    def test_which_bundles_carry_credentials(self, bundle, credentialed_bundle):
        assert not byoc.carries_credentials(byoc.read_baked_env(bundle))
        assert byoc.carries_credentials(byoc.read_baked_env(credentialed_bundle))

    def test_an_account_without_its_password(self, credentialed_bundle, tmp_path):
        env = "\n".join(l for l in _read(credentialed_bundle, ".env").splitlines()
                        if not l.startswith("ARANGO_PASSWORD="))
        bad = _rewrite(credentialed_bundle, tmp_path / "nopw.tar.gz", {".env": env})
        with pytest.raises(byoc.DeployError, match="ARANGO_PASSWORD missing"):
            byoc.preflight(bad, "chronograph", "testdb")

    def test_an_account_with_platform_login_left_on(self, credentialed_bundle, tmp_path):
        env = _read(credentialed_bundle, ".env").replace("CHRONO_PLATFORM_AUTH=off\n", "")
        bad = _rewrite(credentialed_bundle, tmp_path / "on.tar.gz", {".env": env})
        with pytest.raises(byoc.DeployError, match="platform login on"):
            byoc.preflight(bad, "chronograph", "testdb")

    def test_wrong_mount_path(self, bundle):
        with pytest.raises(byoc.DeployError, match="SERVICE_URL_PATH_PREFIX"):
            byoc.preflight(bundle, "chronograph", "otherdb")

    def test_root_account(self, credentialed_bundle, tmp_path):
        env = _read(credentialed_bundle, ".env").replace("ARANGO_USERNAME=chrono-svc",
                                                         "ARANGO_USERNAME=root")
        bad = _rewrite(credentialed_bundle, tmp_path / "root.tar.gz", {".env": env})
        with pytest.raises(byoc.DeployError, match="root account"):
            byoc.preflight(bad, "chronograph", "testdb")
        byoc.preflight(bad, "chronograph", "testdb", allow_root=True)

    def test_root_absolute_urls(self, bundle, tmp_path):
        html = _read(bundle, "viz/web/index.html").replace('src="./static/', 'src="/static/')
        api_js = _read(bundle, "viz/web/js/api.js").replace("_get('api/", "_get('/api/")
        bad = _rewrite(bundle, tmp_path / "abs.tar.gz",
                       {"viz/web/index.html": html, "viz/web/js/api.js": api_js})
        with pytest.raises(byoc.DeployError) as err:
            byoc.preflight(bad, "chronograph", "testdb")
        assert "index.html" in str(err.value) and "api.js" in str(err.value)

    def test_snapshot_fallback_disabled(self, bundle, tmp_path):
        env = _read(bundle, ".env").replace("CHRONO_SOURCE=arango", "CHRONO_SOURCE=auto")
        bad = _rewrite(bundle, tmp_path / "auto.tar.gz", {".env": env})
        with pytest.raises(byoc.DeployError, match="CHRONO_SOURCE"):
            byoc.preflight(bad, "chronograph", "testdb")


def test_mount_path():
    assert byoc.mount_path("chronograph", "ic-knowledge-graph-temporal") == \
        "/_service/uds/_db/ic-knowledge-graph-temporal/chronograph"
    assert byoc.mount_path("chronograph", None) == "/_service/uds/_global/chronograph"


class _StubPlatform:
    """Stands in for byoc_deploy.Platform: effective permissions by (user, db)."""

    def __init__(self, perms: dict[tuple[str, str], str]):
        self.perms = perms

    def effective_permission(self, user, db_name):
        return self.perms.get((user, db_name), "none")


class TestAccountScope:
    """The baked account is judged by what it can do, not by its name — a
    non-root account with rw on _system is an administrator."""

    def test_scoped_read_only_account_passes(self):
        stub = _StubPlatform({("chronograph", "testdb"): "ro"})
        byoc.check_account_scope(stub, "chronograph", "testdb")

    def test_system_admin_refused_even_if_not_named_root(self):
        stub = _StubPlatform({("some-admin", "_system"): "rw", ("some-admin", "testdb"): "rw"})
        with pytest.raises(byoc.DeployError, match="administrator"):
            byoc.check_account_scope(stub, "some-admin", "testdb")
        byoc.check_account_scope(stub, "some-admin", "testdb", allow_privileged=True)

    def test_account_without_access_refused(self):
        stub = _StubPlatform({})
        with pytest.raises(byoc.DeployError, match="cannot read"):
            byoc.check_account_scope(stub, "chronograph", "testdb")


def test_read_baked_env(credentialed_bundle):
    env = byoc.read_baked_env(credentialed_bundle)
    assert env["ARANGO_USERNAME"] == "chrono-svc" and env["CHRONO_SOURCE"] == "arango"
