# Deploying ChronoGraph to the Arango Platform (BYOC)

**Audience:** whoever hosts the ChronoGraph demo on an Arango platform cluster
with the Container Manager (prod.demo.pilot.arango.ai), instead of — or as well
as — running `python viz/server.py` locally.

ChronoGraph runs on the platform as a *user-defined service* built from a
tarball ("bring your own code") on a platform base image, reached through the
cluster's own gateway. It only **reads** the temporal knowledge graph
(`ic-knowledge-graph-temporal`); nothing here changes the data. The recipe is
ported from `project-sentinel` (`docs/platform-deployment.md`), first verified
live on prod.demo on 2026-09-25.

## 1. The platform contract

| Rule | What it means for ChronoGraph |
| --- | --- |
| Flat tarball, `entrypoint` at the root | `viz/deploy/package.sh` builds it. `viz/deploy/entrypoint` is a Python file whose **line 1 is the literal token `entrypoint`** — the platform runs `python /project/<first word of that file>`. No shebang, docstring or import above it. The bundle mirrors the repo (`./viz/...` + `./.env`) so `ic_viz/datasource.py` finds `.env` exactly where it does locally. |
| Base image per cluster | prod.demo offers `node22base`, `py12base`, `py12cugraph`, `py12torch` (no `py13base`). ChronoGraph uses `py12base` (Python 3.12, **no `uv`** — the entrypoint bootstraps pip). A wrong key fails fast and lists the valid ones. |
| Port 8000, answer `/` | The entrypoint runs uvicorn on `$PORT` or 8000 serving `ic_viz.api:asgi_app`. |
| Mount path | `/_service/uds/_db/<db>/<instance>/` — here `/_service/uds/_db/ic-knowledge-graph-temporal/chronograph/`. The **trailing slash is required**. The ingress forwards the full path; `ic_viz/prefix.py` strips it, and the front-end uses only relative URLs, so no build step depends on the prefix. |
| No app environment | The platform passes the app none of its settings, so `package.sh` bakes a sanitized `.env` from an allowlist — endpoint, database, mount prefix, `CHRONO_SOURCE=arango` — never an `*_API_KEY`. By default it holds **no account** (see *Platform login* below). |
| Gateway auth | Every request, valid path or not, gets `401` until the browser is logged in to the platform. ChronoGraph adds no login screen of its own. |
| Platform login | The gateway forwards the signed-in user's login with each request, and the platform injects the cluster's internal endpoint (`ARANGO_DEPLOYMENT_ENDPOINT`), its CA (`ARANGO_DEPLOYMENT_CA`) and an integration sidecar. ChronoGraph reads the database **as that user** (`viz/ic_viz/platform_auth.py`, `platform.py`, `datasource.PlatformArangoSource`), verifying TLS against the injected CA. So each person needs `ro` (or `rw`) on `ic-knowledge-graph-temporal`; anyone without it gets a `403` naming the database, an expired login a `401`. |
| No in-place update | A second install of the same instance name fails. `update` deletes then recreates; the URL is down for about a minute plus dependency install. |
| Unique package versions | Packages are keyed on `(name, version)`. `update` uploads `<release>-<n>`, where the release is `viz/ic_viz/__init__.py:__version__`. |

## 2. One-time setup

**Default (platform login): nothing to set up in the bundle.** Grant each
person who should see ChronoGraph `ro` on `ic-knowledge-graph-temporal` (their
own platform account). Access is checked once per login and rechecked every
five minutes, so a revoked grant takes effect within that time.

**Only for a cluster without platform login** — build with
`package.sh --with-credentials`, which bakes a service account and turns
platform login off (`CHRONO_PLATFORM_AUTH=off`). That tarball **is a secret**
(gitignored, mode 600), and `package.sh` **refuses to bake `root`** without
`--allow-root`:

1. **A dedicated service account** with access to `ic-knowledge-graph-temporal`
   only — ChronoGraph only reads, so `ro` is enough (`ro` on
   `ic-knowledge-graph-temporal`, `none` on `_system` and every other
   database). Do not use `root` — **or any account with `rw` on `_system`**,
   which is an administrator whatever its name. The bundle is uploaded to the platform's file manager,
   and anyone who can fetch it is one `tar -x` away from the credentials inside.
2. Add it to the repo-root `.env` (git-ignored) **next to** the existing
   `ARANGO_*` entries — those stay as the *deploy* account (used by
   `byoc_deploy.py` to upload, never baked):

   ```bash
   SERVICE_ARANGO_USERNAME=<service user>
   SERVICE_ARANGO_PASSWORD=<its password>
   ```

## 3. Deploy

```bash
viz/deploy/package.sh                                 # -> viz/chronograph-service.tar.gz (~250 KB)
.venv/bin/python viz/deploy/byoc_deploy.py list       # what is uploaded / running
.venv/bin/python viz/deploy/byoc_deploy.py update     # pre-flight, upload, swap, verify
```

`update` pre-flights the tarball (layout, entrypoint token, baked `.env`,
`CHRONO_SOURCE=arango`, prefix vs. mount path, relative URLs in `index.html`
and `api.js`). For a `--with-credentials` bundle it also refuses `root`,
requires `CHRONO_PLATFORM_AUTH=off`, and checks the baked account's
**effective** permissions through the deploy session — it must be able to read
the target database and must not hold `rw` on `_system`. Only then does it
upload the bundle as
`chronograph v<release>-<n>`, swaps
the live service, waits for `DEPLOYED`, then polls the public URL and
deep-verifies: `/healthz` must report the release version **and**
`source: arango`, `/api/repos` must return commit counts from the database
(read as the deploying account, whose login the check sends), and
every asset `index.html` references must serve. Only then does it print
`VERIFIED`.

The service appears in the platform UI as **ChronoGraph** at:

```
https://prod.demo.pilot.arango.ai/_service/uds/_db/ic-knowledge-graph-temporal/chronograph/
```

Log in to the platform first; the gateway answers `401` otherwise.

To see what the platform gives the container — whether the endpoint and CA
were injected, whether TLS verifies, whether your login was forwarded and the
sidecar recognises it (never a token or claim value) — sign in at
`https://prod.demo.pilot.arango.ai/ui/` and open:

```
https://prod.demo.pilot.arango.ai/_service/uds/_db/ic-knowledge-graph-temporal/chronograph/api/platform/diagnostics
```

### Reading the response codes while polling

| code | meaning |
| --- | --- |
| `404` | route not registered yet (ingress lag of a few seconds) or wrong mount path |
| `401` | to an *authenticated* caller: gateway/pod not ready yet (cold start) — flips to `200` |
| `503` | route up, pod not ready — normal for the first minute or two while pip installs on `py12base`; also what a just-deleted service returns |
| `200` | serving |

## 4. Update, roll back, remove

```bash
# new code: bump viz/ic_viz/__init__.py:__version__ (verification compares it)
viz/deploy/package.sh && .venv/bin/python viz/deploy/byoc_deploy.py update

# go back to a package already on the platform (fast, no rebuild; DB untouched)
.venv/bin/python viz/deploy/byoc_deploy.py rollback --to 0.2.0-1

# remove the service
.venv/bin/python viz/deploy/byoc_deploy.py delete
```

Bump the version for any code change: a `200` on the root page is served just
as happily by the build being replaced, and `/healthz` is the only proof the
new one is live.

## 5. Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `401` "did not carry your platform login" / "refused your platform login" | Sign in to the platform again at `/ui/` and reload. |
| `403` "cannot read the 'ic-knowledge-graph-temporal' database" | Your platform account has no access to the database — ask an administrator for `ro`. |
| Pod exits at boot with `no platform login … ARANGO_PASSWORD not set` | The platform did not inject `ARANGO_DEPLOYMENT_ENDPOINT` (or `CHRONO_PLATFORM_AUTH=off`). On such a cluster build with `--with-credentials`. |
| `refusing to bake the root account` | (`--with-credentials` only.) Add `SERVICE_ARANGO_USERNAME`/`SERVICE_ARANGO_PASSWORD` to `.env` (§2). `--allow-root` exists but should be a deliberate exception. |
| `has rw on _system (administrator)` | (`--with-credentials` only.) The service account is an admin under another name. Use a dedicated account scoped to the database (§2). |
| `cannot read the graph` | The service account lacks access to the target database — grant it `ro` there. |
| `No entrypoint found` | Nested tar layout. `tar -tzf viz/chronograph-service.tar.gz \| head` must show `./entrypoint`. |
| `python /project/"""` in the logs | Line 1 of `entrypoint` is not the bare token. |
| Blank page, assets `404` | Root-absolute URLs (`/static/...`, `/api/...`) or a `SERVICE_URL_PATH_PREFIX` that differs from the mount path. `tests/test_viz_platform.py` and the pre-flight both guard this; rebuild with matching `--instance`/`--db`. |
| Pod crash-loops at boot with `CHRONO_SOURCE=arango but live DB unreachable` | (`--with-credentials` only.) The baked service account cannot log in or lacks access to the database. Deliberate: no offline snapshot ships, so the app fails loudly instead of serving nothing. |
| `/healthz` shows `source: snapshot` | A bundle built without `CHRONO_SOURCE=arango` — rebuild with `package.sh` (the pre-flight rejects this). |

## 6. Local rehearsal of the bundle

Boots the real artifact through its own entrypoint under the mount prefix
(dependencies taken from the dev venv). Locally there is no platform login, so
rehearse a `--with-credentials` bundle:

```bash
viz/deploy/package.sh /tmp/b.tar.gz --with-credentials && mkdir -p /tmp/b && tar -xzf /tmp/b.tar.gz -C /tmp/b
env -i PATH="$PATH" HOME="$HOME" CHRONO_SKIP_INSTALL=1 PORT=8148 .venv/bin/python /tmp/b/entrypoint
# then open http://127.0.0.1:8148/_service/uds/_db/ic-knowledge-graph-temporal/chronograph/
rm -rf /tmp/b /tmp/b.tar.gz   # the bundle carries credentials
```

## 7. Files

| Concern | Path |
| --- | --- |
| Platform login | `viz/ic_viz/platform_auth.py`, `viz/ic_viz/platform.py`, `PlatformArangoSource` in `viz/ic_viz/datasource.py` |
| Prefix middleware | `viz/ic_viz/prefix.py` (wired as `asgi_app` in `viz/ic_viz/api.py`) |
| Release proof | `/healthz` in `viz/ic_viz/api.py`; version in `viz/ic_viz/__init__.py` |
| Platform entrypoint | `viz/deploy/entrypoint` |
| Bundle builder | `viz/deploy/package.sh` |
| Deploy CLI | `viz/deploy/byoc_deploy.py` |
| Tests | `tests/test_viz_platform.py`, `tests/test_viz_platform_login.py` |
| Origin of the recipe | `project-sentinel/docs/platform-deployment.md` → `arango-ontoextract/docs/container-manager-deployment.md` |
