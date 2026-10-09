#!/usr/bin/env bash
# Build the ChronoGraph BYOC bundle for the Arango Container Manager.
#
# Output: a FLAT tar.gz (entrypoint at the archive root). The layout mirrors the
# repo so ic_viz/datasource.py finds .env exactly where it does locally:
#   entrypoint            viz/deploy/entrypoint (line 1 token rule)
#   pyproject.toml        GENERATED: name, version (ic_viz.__version__), deps
#   requirements.txt      viz/requirements.txt
#   README.md             viz/README.md
#   viz/ic_viz/, viz/web/ the app (no __pycache__, no snapshot — live DB only)
#   .env                  SANITIZED allowlist: endpoint, database, mount prefix,
#                         CHRONO_SOURCE=arango. Never an *_API_KEY. By default no
#                         account either: on the platform ChronoGraph reads as the
#                         signed-in user (viz/ic_viz/platform_auth.py), so each
#                         person needs read access to the database.
#
# --with-credentials builds the older kind of bundle, with an account baked in
# and platform login off (CHRONO_PLATFORM_AUTH=off); that tarball IS A SECRET
# (gitignored; do not share it).
#
# Credentials baked (--with-credentials only): SERVICE_ARANGO_USERNAME / SERVICE_ARANGO_PASSWORD (env or
# repo-root .env) — a dedicated low-privilege account. Without them it falls back
# to ARANGO_USERNAME / ARANGO_PASSWORD but REFUSES to bake `root` unless
# --allow-root is given (ChronoGraph only reads; root on a shared cluster would
# put every database on it one `tar -x` away from anyone holding the bundle).
#
# Usage:
#   viz/deploy/package.sh [OUT] [--instance NAME] [--db NAME] [--global] [--with-credentials [--allow-root]]
set -euo pipefail
export COPYFILE_DISABLE=1   # no AppleDouble / xattr PAX headers on macOS

VIZ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${VIZ_DIR}/.." && pwd)"
OUT="${VIZ_DIR}/chronograph-service.tar.gz"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"   # override for hermetic tests
INSTANCE="${INSTANCE:-chronograph}"
DB="${DB:-}"
GLOBAL=0
ALLOW_ROOT=0
WITH_CREDENTIALS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --instance) INSTANCE="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --global) GLOBAL=1; shift ;;
    --allow-root) ALLOW_ROOT=1; shift ;;
    --with-credentials) WITH_CREDENTIALS=1; shift ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) OUT="$1"; shift ;;
  esac
done

# --- resolve settings: environment first, then repo-root .env (uncommented lines) --
envval() {  # envval KEY -> value from environ, else from .env (quotes stripped)
  local key="$1" val="${!1:-}"
  if [[ -z "${val}" && -f "${ENV_FILE}" ]]; then
    val="$(grep -E "^${key}=" "${ENV_FILE}" | tail -1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
  fi
  printf '%s' "${val}"
}
ENDPOINT="$(envval ARANGO_ENDPOINT)"
[[ -n "${DB}" ]] || DB="$(envval ARANGO_DATABASE)"
[[ -n "${ENDPOINT}" && -n "${DB}" ]] || {
  echo "error: need ARANGO_ENDPOINT and ARANGO_DATABASE (env or .env)" >&2; exit 1; }
case "${ENDPOINT}" in *localhost*|*127.0.0.1*)
  echo "error: ARANGO_ENDPOINT=${ENDPOINT} is loopback — unreachable from the platform" >&2; exit 1 ;;
esac
USER_=""; PASSWORD=""
if [[ "${WITH_CREDENTIALS}" == "1" ]]; then
  USER_="$(envval SERVICE_ARANGO_USERNAME)"
  PASSWORD="$(envval SERVICE_ARANGO_PASSWORD)"
  if [[ -z "${USER_}" || -z "${PASSWORD}" ]]; then
    USER_="$(envval ARANGO_USERNAME)"; [[ -n "${USER_}" ]] || USER_="$(envval ARANGO_USER)"
    PASSWORD="$(envval ARANGO_PASSWORD)"
    echo "warning: SERVICE_ARANGO_USERNAME/PASSWORD not set — falling back to ARANGO_USERNAME (${USER_})" >&2
  fi
  [[ -n "${USER_}" && -n "${PASSWORD}" ]] || {
    echo "error: --with-credentials needs service credentials (env or .env)" >&2; exit 1; }
fi
if [[ "${WITH_CREDENTIALS}" == "1" && "${USER_}" == "root" && "${ALLOW_ROOT}" != "1" ]]; then
  echo "error: refusing to bake the root account into the bundle. Set" >&2
  echo "       SERVICE_ARANGO_USERNAME / SERVICE_ARANGO_PASSWORD in .env to a dedicated" >&2
  echo "       account with access to ${DB} only, or pass --allow-root deliberately." >&2
  exit 1
fi

if [[ "${GLOBAL}" == "1" ]]; then PREFIX="/_service/uds/_global/${INSTANCE}"
else PREFIX="/_service/uds/_db/${DB}/${INSTANCE}"; fi

VERSION="$(sed -nE 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']([^"'"'"']+)["'"'"'].*/\1/p' "${VIZ_DIR}/ic_viz/__init__.py")"
[[ -n "${VERSION}" ]] || { echo "error: no __version__ in viz/ic_viz/__init__.py" >&2; exit 1; }

# --- stage --------------------------------------------------------------------
STAGE="$(mktemp -d)"; trap 'rm -rf "${STAGE}"' EXIT
cp "${VIZ_DIR}/deploy/entrypoint" "${STAGE}/entrypoint"; chmod +x "${STAGE}/entrypoint"
head -1 "${STAGE}/entrypoint" | grep -q '^entrypoint' || { echo "error: entrypoint line 1 must start with 'entrypoint'" >&2; exit 1; }
cp "${VIZ_DIR}/requirements.txt" "${STAGE}/requirements.txt"
cp "${VIZ_DIR}/README.md" "${STAGE}/README.md"
mkdir -p "${STAGE}/viz"
# tar-pipe excludes caches portably (rsync is not guaranteed).
(cd "${VIZ_DIR}" && tar -cf - --exclude='__pycache__' --exclude='*.pyc' --exclude='.DS_Store' ic_viz web) \
  | (cd "${STAGE}/viz" && tar -xf -)

# pyproject.toml: metadata + the SAME dependency list as requirements.txt, and
# no installable packages (the entrypoint runs the app from ./viz in place).
{
  echo "# Generated by viz/deploy/package.sh — do not edit; source: viz/requirements.txt"
  echo "[build-system]"
  echo 'requires = ["setuptools>=68"]'
  echo 'build-backend = "setuptools.build_meta"'
  echo
  echo "[project]"
  echo 'name = "chronograph"'
  echo "version = \"${VERSION}\""
  echo 'description = "ChronoGraph — temporal / provenance visualizer for the IC design knowledge graph"'
  echo 'requires-python = ">=3.11"'
  echo "dependencies = ["
  grep -vE '^\s*(#|$)' "${VIZ_DIR}/requirements.txt" | sed -E 's/^[[:space:]]*(.*[^[:space:]])[[:space:]]*$/  "\1",/'
  echo "]"
  echo
  echo "[tool.setuptools]"
  echo "packages = []"
} > "${STAGE}/pyproject.toml"

{
  echo "# ChronoGraph — baked by viz/deploy/package.sh (sanitized: connection keys only)"
  echo "ARANGO_ENDPOINT=${ENDPOINT}"
  if [[ "${WITH_CREDENTIALS}" == "1" ]]; then
    echo "ARANGO_USERNAME=${USER_}"
    echo "ARANGO_PASSWORD=${PASSWORD}"
    echo "CHRONO_PLATFORM_AUTH=off"
  fi
  echo "ARANGO_DATABASE=${DB}"
  echo "SERVICE_URL_PATH_PREFIX=${PREFIX}"
  echo "CHRONO_SOURCE=arango"
} > "${STAGE}/.env"
if grep -qiE '_API_KEY=.+' "${STAGE}/.env"; then echo "error: an API key leaked into the baked .env" >&2; exit 1; fi

if [[ "$(uname -s)" == "Darwin" ]] && command -v xattr >/dev/null 2>&1; then xattr -cr "${STAGE}" 2>/dev/null || true; fi
tar -czf "${OUT}" -C "${STAGE}" .
chmod 600 "${OUT}"
SIZE="$(du -h "${OUT}" | cut -f1)"
if [[ "${WITH_CREDENTIALS}" == "1" ]]; then
  echo "Wrote ${OUT} (${SIZE}, flat layout, mode 600 — contains credentials; platform login off)"
else
  echo "Wrote ${OUT} (${SIZE}, flat layout — no credentials; reads as the signed-in platform user)"
fi
echo "  release: ${VERSION}   instance: ${INSTANCE}   db: ${DB}   user: ${USER_:-(each signed-in platform user)}"
echo "  mount: ${PREFIX}/"
echo "  .env keys: $(grep -vE '^#' "${STAGE}/.env" | cut -d= -f1 | tr '\n' ' ')"
