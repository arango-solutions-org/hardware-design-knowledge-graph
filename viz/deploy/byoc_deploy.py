#!/usr/bin/env python3
"""Upload and deploy ChronoGraph to the Arango Platform Container Manager (BYOC).

Companion to ``viz/deploy/package.sh``, which builds the bundle this uploads.
Ported from project-sentinel's ``scripts/byoc_deploy.py`` (itself from
arango-ontoextract; the platform-shaped half is identical for every project and
was verified live on prod.demo.pilot.arango.ai). ChronoGraph-specific parts are
the defaults, the version source, the pre-flight checks and the verifier.

Usage
-----
    python viz/deploy/byoc_deploy.py list
    python viz/deploy/byoc_deploy.py update                 # pre-flight, upload, swap, verify
    python viz/deploy/byoc_deploy.py verify [--expect-version 0.2.0]
    python viz/deploy/byoc_deploy.py rollback --to 0.2.0-1
    python viz/deploy/byoc_deploy.py delete
    python viz/deploy/byoc_deploy.py status --service-id arango-user-defined-xxxxx

Platform contract (viz/DEPLOY.md): flat tarball with a Python ``entrypoint`` at
its root; the service listens on port 8000 and is mounted at
``/_service/uds/_db/<db>/<instance>/`` (trailing slash required); the platform
injects no app environment, so settings travel baked in the bundle's ``.env``.
By default that ``.env`` holds no account: on the platform ChronoGraph reads as
the signed-in user. A bundle built with ``package.sh --with-credentials``
carries a service account instead, and only then is that account checked.
There is no in-place update: ``update`` deletes and recreates, and the service
is gone for about a minute. Package versions are unique per name.

The DEPLOY itself authenticates with the repo-root ``.env`` account
(``ARANGO_ENDPOINT``, ``ARANGO_USERNAME``, ``ARANGO_PASSWORD``,
``ARANGO_DATABASE``) — that account is never baked; only package.sh's
``SERVICE_ARANGO_*`` service account travels in a ``--with-credentials`` bundle. Nothing here
writes a token to disk.
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import requests

VIZ_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = VIZ_DIR.parent
DEFAULT_TARBALL = VIZ_DIR / "chronograph-service.tar.gz"
DEFAULT_APP_NAME = "chronograph"
DEFAULT_INSTANCE = "chronograph"
# prod.demo offers node22base, py12base, py12cugraph, py12torch, test (no py13base).
# A wrong key fails fast and the error lists the valid ones.
DEFAULT_BASE_IMAGE = "py12base"
DEFAULT_DISPLAY_NAME = "ChronoGraph"
DEFAULT_DESCRIPTION = (
    "Temporal / provenance explorer for the IC design knowledge graph: time-travel "
    "across design epochs, spec-to-RTL provenance, cross-project lineage."
)

ACP = "/_platform/acp/v1"
FILEMANAGER = "/_platform/filemanager/global/byoc/"
READY = {"DEPLOYED"}
FAILED = {"FAILED", "ERROR", "TERMINATED"}
SECRET_KEY_PATTERN = re.compile(r"^[A-Z0-9_]*_API_KEY\s*=\s*\S", re.M)


class DeployError(RuntimeError):
    pass


def load_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file; surrounding quotes are stripped (a quoted password
    otherwise surfaces as an unexplained 401)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def tar_member_name(name: str) -> str:
    """Member name without a leading ``./`` (never ``lstrip('./')`` — it eats ``.env``)."""
    return name[2:] if name.startswith("./") else name


def mount_path(instance: str, db_name: str | None) -> str:
    """Public prefix the platform serves this instance under. Must equal the
    SERVICE_URL_PATH_PREFIX baked into the bundle's .env."""
    scope = f"_db/{db_name}" if db_name else "_global"
    return f"/_service/uds/{scope}/{instance}"


def read_app_version() -> str:
    """Release version from ``viz/ic_viz/__init__.py`` (parsed, so no venv needed)."""
    text = (VIZ_DIR / "ic_viz" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not match:
        raise DeployError("no __version__ in viz/ic_viz/__init__.py")
    return match.group(1)


def _service_id_of(result: dict) -> tuple[str | None, str | None]:
    """(serviceId, status) from a deploy/status response; the create response
    nests them under ``serviceInfo``."""
    info = result.get("serviceInfo") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        info = result if isinstance(result, dict) else {}
    return info.get("serviceId") or info.get("service_id"), info.get("status")


class Platform:
    """Thin client over the Container Manager endpoints used by a BYOC release."""

    def __init__(self, base: str, user: str, password: str, *, timeout: float = 60.0):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self._jwt: str | None = None

    def authenticate(self) -> None:
        response = self.session.post(
            f"{self.base}/_open/auth",
            json={"username": self.user, "password": self.password},
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise DeployError(f"auth failed: HTTP {response.status_code}")
        token = response.json().get("jwt")
        if not token:
            raise DeployError("auth response carried no 'jwt' field")
        self._jwt = token

    def _headers(self) -> dict[str, str]:
        if self._jwt is None:
            self.authenticate()
        return {"Authorization": f"Bearer {self._jwt}"}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self.timeout)
        url = f"{self.base}{path}"
        response = self.session.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code == 401:  # one transparent re-auth (slow uploads)
            self.authenticate()
            response = self.session.request(method, url, headers=self._headers(), **kwargs)
        if response.status_code >= 400:
            raise DeployError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:400]}")
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text[:400]}

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        return self.session.get(url, headers=self._headers(), **kwargs)

    def list_packages(self) -> list[dict]:
        return self._request("GET", FILEMANAGER).get("services", [])

    def list_services(self) -> list[dict]:
        return self._request("POST", f"{ACP}/list_services", json={}).get("services", [])

    def upload(self, tarball: Path, name: str, version: str) -> dict:
        with tarball.open("rb") as handle:
            return self._request(
                "POST", FILEMANAGER,
                data={"name": name, "version": version, "language": "python", "type": "Service"},
                files={"file": (tarball.name, handle, "application/gzip")},
                timeout=600,
            )

    def deploy(self, name: str, version: str, instance: str, db_name: str | None,
               base_image: str, *, has_ui: bool = True, display_name: str | None = None,
               description: str | None = None) -> dict:
        # Every value must be a string: the platform decodes `env` as a string map.
        env: dict[str, str] = {"service_type": "base_type", "base_image": base_image,
                               "app_instance_name": instance}
        if db_name:
            env["db_name"] = db_name
        if has_ui:
            env["has_ui"] = "true"
        if display_name:
            env["display_name"] = display_name
        if description:
            env["description"] = description
        return self._request("POST", f"{ACP}/uds",
                             json={"app_name": name, "app_version": version, "env": env},
                             timeout=180)

    def effective_permission(self, user: str, db_name: str) -> str:
        """Effective database access level (rw/ro/none) of ``user``, read via the deploy account."""
        return str(self._request("GET", f"/_db/_system/_api/user/{user}/database/{db_name}").get("result"))

    def service_status(self, service_id: str) -> dict:
        return self._request("GET", f"{ACP}/service/{service_id}")

    def delete_service(self, service_id: str) -> None:
        self._request("DELETE", f"{ACP}/service/{service_id}", timeout=120)

    def find_instances(self, instance: str) -> list[dict]:
        found = []
        for service in self.list_services():
            uds = ((service.get("serviceMeta") or {}).get("udsMeta")) or {}
            if uds.get("appInstanceName") == instance:
                found.append({"serviceId": service.get("serviceId"), "version": uds.get("version"),
                              "status": service.get("status"), "dbName": service.get("dbName")})
        return found

    def resolve_instance(self, instance: str) -> dict | None:
        matches = self.find_instances(instance)
        if len(matches) > 1:
            raise DeployError(f"{len(matches)} services run as instance {instance!r}: "
                              f"{[m['serviceId'] for m in matches]}. Delete the extras by id first.")
        return matches[0] if matches else None

    def wait_until_ready(self, service_id: str, *, timeout_s: float = 600.0,
                         interval_s: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout_s
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.service_status(service_id)
            info = last.get("serviceInfo") if isinstance(last, dict) else {}
            info = info if isinstance(info, dict) else {}
            state = str(info.get("status") or last.get("status") or "").upper()
            if state in READY:
                return last
            if state in FAILED:
                raise DeployError(f"service {service_id} reached {state}: {last}")
            print(f"    status={state or '(unknown)'} — waiting {interval_s:.0f}s", flush=True)
            time.sleep(interval_s)
        raise DeployError(f"timed out after {timeout_s:.0f}s; last status: {last}")


def next_build_version(platform: Platform, name: str, release: str) -> str:
    """``<release>-<n>``: first suffix not already uploaded under ``name``."""
    taken = {p["version"] for p in platform.list_packages() if p.get("name") == name}
    for build in range(1, 1000):
        candidate = f"{release}-{build}"
        if candidate not in taken:
            return candidate
    raise DeployError(f"no free build suffix for {release}")


REQUIRED_MEMBERS = (
    "entrypoint", "pyproject.toml", "requirements.txt",
    "viz/ic_viz/api.py", "viz/ic_viz/datasource.py", "viz/ic_viz/prefix.py",
    "viz/ic_viz/build_snapshot.py", "viz/web/index.html", "viz/web/js/api.js",
    "viz/web/js/app.js", "viz/web/vendor/cytoscape.min.js",
)


def read_baked_env(tarball: Path) -> dict[str, str]:
    """The sanitized .env baked into the bundle (empty dict if absent)."""
    with tarfile.open(tarball, "r:gz") as archive:
        for raw in archive.getnames():
            if tar_member_name(raw) == ".env":
                handle = archive.extractfile(raw)
                text = handle.read().decode("utf-8", "replace") if handle else ""
                return {k.strip(): v.strip() for k, v in
                        (l.split("=", 1) for l in text.splitlines() if "=" in l and not l.startswith("#"))}
    return {}


def carries_credentials(env: dict[str, str]) -> bool:
    """Whether a baked ``.env`` holds an account (a ``--with-credentials``
    bundle) rather than relying on the signed-in platform user."""
    return bool(env.get("ARANGO_USERNAME") or env.get("ARANGO_PASSWORD"))


def check_account_scope(platform: Platform, user: str, db_name: str | None, *,
                        allow_privileged: bool = False) -> None:
    """Judge the baked account by what it can DO, not by its name: a non-root
    account holding rw on _system is an administrator (user and database
    management, and rw on every database). Also refuse an account that cannot read the target database —
    the pod would boot and crash-loop on CHRONO_SOURCE=arango."""
    system = platform.effective_permission(user, "_system")
    target = platform.effective_permission(user, db_name) if db_name else "n/a"
    print(f"    service account {user!r}: {db_name or '(global)'}={target}, _system={system}")
    if db_name and target not in ("ro", "rw"):
        raise DeployError(f"baked account {user!r} has {target!r} on {db_name} — it cannot read the graph")
    if system == "rw" and not allow_privileged:
        raise DeployError(f"baked account {user!r} has rw on _system (administrator) — anyone holding "
                          f"the bundle could manage users and every database. Use a dedicated "
                          f"account scoped to {db_name} (viz/DEPLOY.md §2), or pass --allow-root deliberately.")


def preflight(tarball: Path, instance: str, db_name: str | None, *, allow_root: bool = False) -> None:
    """Refuse to upload an artifact that cannot work (or should not leave the laptop)."""
    if not tarball.exists():
        raise DeployError(f"no tarball at {tarball} — run viz/deploy/package.sh first")
    problems: list[str] = []
    with tarfile.open(tarball, "r:gz") as archive:
        names = {tar_member_name(n): n for n in archive.getnames()}

        def read(member: str) -> str:
            raw = names.get(member)
            if not raw:
                return ""
            handle = archive.extractfile(raw)
            return handle.read().decode("utf-8", "replace") if handle else ""

        for required in REQUIRED_MEMBERS:
            if required not in names:
                problems.append(f"{required} missing from the archive root layout")
        entry = read("entrypoint")
        if entry and not entry.splitlines()[0].startswith("entrypoint"):
            problems.append("entrypoint line 1 must start with the token `entrypoint`")
        if any("__pycache__" in n for n in names):
            problems.append("__pycache__ shipped in the bundle")

        env_text = read(".env")
        if not env_text:
            problems.append(".env is not baked — the platform injects no app settings, so the "
                            "service would not know its database or mount path")
        else:
            env = {k.strip(): v.strip() for k, v in
                   (l.split("=", 1) for l in env_text.splitlines() if "=" in l and not l.startswith("#"))}
            required_keys = ["ARANGO_ENDPOINT", "ARANGO_DATABASE"]
            if carries_credentials(env):
                required_keys += ["ARANGO_USERNAME", "ARANGO_PASSWORD"]
                if env.get("CHRONO_PLATFORM_AUTH", "").lower() not in ("off", "0", "false", "no"):
                    problems.append("the bundle carries an account but leaves platform login on — "
                                    "rebuild with package.sh --with-credentials (it sets "
                                    "CHRONO_PLATFORM_AUTH=off)")
            for key in required_keys:
                if not env.get(key):
                    problems.append(f"{key} missing from the baked .env")
            if re.search(r"localhost|127\.0\.0\.1", env.get("ARANGO_ENDPOINT", "")):
                problems.append("ARANGO_ENDPOINT points at loopback — unreachable from the platform")
            if env.get("ARANGO_USERNAME") == "root" and not allow_root:
                problems.append("the bundle carries the root account — rebuild with a dedicated "
                                "SERVICE_ARANGO_USERNAME (or pass --allow-root deliberately)")
            if env.get("CHRONO_SOURCE") != "arango":
                problems.append("CHRONO_SOURCE must be 'arango' — no snapshot ships, so a silent "
                                "fallback would crash instead of failing loudly")
            if SECRET_KEY_PATTERN.search(env_text):
                problems.append("an *_API_KEY is baked into .env — rebuild with package.sh (sanitized)")
            expected = mount_path(instance, db_name)
            if env.get("SERVICE_URL_PATH_PREFIX", "").rstrip("/") != expected:
                problems.append(f"SERVICE_URL_PATH_PREFIX={env.get('SERVICE_URL_PATH_PREFIX')!r} "
                                f"but the deploy will mount at {expected!r} — rebuild with "
                                f"--instance/--db matching")
        index = read("viz/web/index.html")
        if index and re.search(r'(?:href|src)="/(?:static|api)', index):
            problems.append("index.html uses root-absolute asset URLs — they break under the mount path")
        api_js = read("viz/web/js/api.js")
        if api_js and re.search(r"['\"`]/api/", api_js):
            problems.append("web/js/api.js calls root-absolute /api/... — breaks under the mount path")
    if problems:
        raise DeployError("pre-flight failed:\n  - " + "\n  - ".join(problems))
    print("    pre-flight OK (layout, entrypoint, baked .env, login, mount prefix, relative URLs)")


def resolve_config(args: argparse.Namespace) -> tuple[Platform, str, str | None]:
    env = load_env(REPO_ROOT / ".env")
    endpoint = args.endpoint or env.get("ARANGO_ENDPOINT")
    user = env.get("ARANGO_USERNAME") or env.get("ARANGO_USER")
    password = env.get("ARANGO_PASSWORD")
    if not endpoint or not user or not password:
        raise DeployError("need ARANGO_ENDPOINT, ARANGO_USERNAME and ARANGO_PASSWORD in .env")
    default_db = env.get("ARANGO_DATABASE") or "ic-knowledge-graph-temporal"
    db_name = default_db if args.db is None else (args.db or None)
    return Platform(endpoint, user, password), endpoint, db_name


def cmd_list(args: argparse.Namespace) -> int:
    platform, endpoint, _ = resolve_config(args)
    print(f"platform: {endpoint}\n\nuploaded packages (most recent first):")
    for package in platform.list_packages()[:15]:
        print(f"  {package['name']:<34} v{package['version']:<12} {package.get('file_name', '')}")
    print("\ndeployed user-defined services:")
    for service in platform.list_services():
        meta = service.get("serviceMeta") or {}
        if str(meta.get("serviceType", "")).startswith("arango-user-defined"):
            uds = meta.get("udsMeta") or {}
            print(f"  {service.get('serviceId'):<36} db={service.get('dbName') or '(global)':<28} "
                  f"instance={uds.get('appInstanceName')} v{uds.get('version')} {service.get('status')}")
    return 0


def deep_verify(platform: Platform, url: str, expect_version: str | None) -> bool:
    """Prove the *right code* is live and reading the live graph: version and
    data source via /healthz, real data via /api/repos, and every asset the page
    references (a prefix mismatch shows a blank page with a green light)."""
    ok = True
    try:
        health = platform.get(url + "healthz").json()
        print(f"    /healthz         {health}")
        if expect_version and health.get("version") != expect_version:
            print(f"    FAIL: live version is {health.get('version')}, expected {expect_version}",
                  file=sys.stderr)
            ok = False
        if health.get("source") != "arango":
            print(f"    FAIL: serving from {health.get('source')!r}, not the live graph", file=sys.stderr)
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL: /healthz unreadable: {exc}", file=sys.stderr)
        return False
    try:
        repos = platform.get(url + "api/repos", timeout=90).json().get("repos", [])
        commits = {r.get("name"): r.get("commit_count") for r in repos}
        print(f"    /api/repos       {len(repos)} repos, commits={commits}")
        if not repos or not any(commits.values()):
            print("    FAIL: /api/repos returned no data from the database", file=sys.stderr)
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL: /api/repos unreadable: {exc}", file=sys.stderr)
        ok = False
    try:
        html = platform.get(url).text
        assets = list(dict.fromkeys(re.findall(r'(?:src|href)="(\./[^"]+)"', html)))
        broken = []
        for asset in assets:
            response = platform.get(url + asset[2:])
            if response.status_code != 200:
                broken.append((response.status_code, asset))
        print(f"    assets           {len(assets) - len(broken)}/{len(assets)} served")
        for code, asset in broken:
            print(f"    FAIL: {code} {asset}", file=sys.stderr)
            ok = False
        if not assets:
            print("    FAIL: index references no relative assets — wrong page served?", file=sys.stderr)
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"    FAIL: could not check assets: {exc}", file=sys.stderr)
        ok = False
    print("    => VERIFIED" if ok else "    => VERIFICATION FAILED")
    return ok


def cmd_verify(args: argparse.Namespace) -> int:
    """Poll the public URL until the pod serves (404 = route not registered yet;
    401/503 = pod not ready — 503 is also what a freshly deleted service returns)."""
    platform, endpoint, db_name = resolve_config(args)
    platform.authenticate()
    url = f"{endpoint}{mount_path(args.instance, db_name)}/"
    print(f"==> polling {url}")
    deadline = time.monotonic() + args.wait_timeout
    while time.monotonic() < deadline:
        try:
            response = platform.get(url, allow_redirects=False)
            code = response.status_code
            if code == 200:
                print(f"    HTTP 200 — serving ({response.headers.get('content-type', '?')})")
                return 0 if deep_verify(platform, url, args.expect_version) else 1
            # Observed 2026-09-25 on prod.demo (project-sentinel): the gateway answers
            # 401 to an authenticated caller until the pod is ready, then flips to 200
            # — so 401 is a cold-start signal here, not an auth failure.
            hint = {404: "route not registered", 401: "gateway/pod not ready yet",
                    503: "pod not ready"}.get(code, "not serving yet")
            print(f"    HTTP {code} ({hint}) — retrying in {args.poll_interval:.0f}s", flush=True)
        except requests.RequestException as exc:
            print(f"    {type(exc).__name__} — retrying in {args.poll_interval:.0f}s", flush=True)
        time.sleep(args.poll_interval)
    print(f"error: {url} never returned 200 within {args.wait_timeout:.0f}s", file=sys.stderr)
    return 1


def _swap(platform: Platform, args: argparse.Namespace, db_name: str | None, version: str) -> int:
    """Delete-then-create — what an update *is* on this platform (a second
    install cannot take over the first one's Kubernetes objects)."""
    existing = platform.resolve_instance(args.instance)
    if existing:
        print(f"==> replacing {existing['serviceId']} (version {existing['version']} -> {version})")
        platform.delete_service(existing["serviceId"])
        print("    old service deleted — the URL is down from here")
    else:
        print(f"==> no existing instance {args.instance!r}; creating fresh")
    print(f"    will mount at: {mount_path(args.instance, db_name)}/   base image: {args.base_image}")
    result = platform.deploy(args.name, version, args.instance, db_name, args.base_image,
                             has_ui=not args.no_ui, display_name=args.display_name,
                             description=args.description)
    service_id, state = _service_id_of(result)
    print(f"    created {service_id} status={state}")
    if service_id:
        print("==> waiting for DEPLOYED...")
        platform.wait_until_ready(service_id, timeout_s=args.wait_timeout)
        print("    DEPLOYED (the pod may still be installing dependencies)")
    args.expect_version = getattr(args, "expect_version", None)
    args.poll_interval = getattr(args, "poll_interval", 15.0)
    return cmd_verify(args)


def cmd_update(args: argparse.Namespace) -> int:
    """Pre-flight, upload, swap, verify. Upload precedes delete so a rejected
    artifact fails while the old service still serves."""
    platform, _endpoint, db_name = resolve_config(args)
    tarball = Path(args.tarball)
    release = read_app_version()
    print(f"==> release {release} (viz/ic_viz/__init__.py)")
    preflight(tarball, args.instance, db_name, allow_root=args.allow_root)
    baked = read_baked_env(tarball)
    if carries_credentials(baked):
        check_account_scope(platform, baked.get("ARANGO_USERNAME", ""), db_name,
                            allow_privileged=args.allow_root)
    else:
        print(f"    login: each signed-in platform user (each needs ro or rw on {db_name or 'the database'})")
    version = args.version or next_build_version(platform, args.name, release)
    print(f"==> uploading {tarball.name} ({tarball.stat().st_size / 1_048_576:.1f} MB) as {args.name} v{version}")
    platform.upload(tarball, args.name, version)
    print("    uploaded")
    args.expect_version = release
    return _swap(platform, args, db_name, version)


def cmd_rollback(args: argparse.Namespace) -> int:
    platform, _endpoint, db_name = resolve_config(args)
    available = sorted({p["version"] for p in platform.list_packages() if p.get("name") == args.name})
    if args.to not in available:
        raise DeployError(f"{args.name} v{args.to} is not uploaded. Available: {available[-10:]}")
    print("==> ROLLBACK to an already-uploaded package (code only; the database is untouched)")
    args.expect_version = None
    return _swap(platform, args, db_name, args.to)


def cmd_delete(args: argparse.Namespace) -> int:
    platform, _, _ = resolve_config(args)
    existing = platform.resolve_instance(args.instance)
    if not existing:
        print(f"no service runs as instance {args.instance!r} — nothing to delete")
        return 0
    print(f"==> deleting {existing['serviceId']} (instance {args.instance}, version {existing['version']})")
    platform.delete_service(existing["serviceId"])
    print("    deleted")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    platform, _, _ = resolve_config(args)
    print(platform.service_status(args.service_id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--endpoint", help="platform URL (default: ARANGO_ENDPOINT)")
    parser.add_argument("--db", default=None,
                        help="database scope; '' for global (default: ARANGO_DATABASE)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_deploy_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--name", default=DEFAULT_APP_NAME)
        p.add_argument("--instance", default=DEFAULT_INSTANCE)
        p.add_argument("--base-image", default=DEFAULT_BASE_IMAGE)
        p.add_argument("--no-ui", action="store_true", help="register as a bare endpoint")
        p.add_argument("--display-name", default=DEFAULT_DISPLAY_NAME)
        p.add_argument("--description", default=DEFAULT_DESCRIPTION)
        p.add_argument("--wait-timeout", type=float, default=900.0)
        p.add_argument("--poll-interval", type=float, default=15.0)

    p = sub.add_parser("list", help="show uploaded packages and deployed services")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("update", help="pre-flight, upload, swap the live service, verify")
    p.add_argument("--version", default=None, help="package version (default: <release>-<next build>)")
    p.add_argument("--tarball", default=str(DEFAULT_TARBALL))
    p.add_argument("--allow-root", action="store_true",
                   help="accept a bundle carrying root or another _system administrator (not recommended)")
    add_deploy_args(p)
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("verify", help="poll the public URL until it serves, then deep-check")
    p.add_argument("--instance", default=DEFAULT_INSTANCE)
    p.add_argument("--wait-timeout", type=float, default=900.0)
    p.add_argument("--poll-interval", type=float, default=20.0)
    p.add_argument("--expect-version", default=None, help="assert /healthz reports this release")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("rollback", help="redeploy a previously uploaded package version")
    p.add_argument("--to", required=True, help="package version to go back to")
    add_deploy_args(p)
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("delete", help="remove the deployed service")
    p.add_argument("--instance", default=DEFAULT_INSTANCE)
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("status", help="raw status of one service id")
    p.add_argument("--service-id", required=True)
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DeployError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
