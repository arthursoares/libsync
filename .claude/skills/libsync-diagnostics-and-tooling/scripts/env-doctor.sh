#!/usr/bin/env bash
# env-doctor.sh — PASS/FAIL/WARN health check of the libsync dev
# environment. Read-only: performs no installs, no network calls, no
# writes.
#
# Usage: env-doctor.sh   (run from anywhere inside the repo)
#
# CI reference pins (as of 2026-07-03, .github/workflows/pytest.yml):
#   Python 3.12, Node 20, Poetry 1.8.0. pyproject.toml allows
#   python >=3.10 <4.0, so other versions WARN rather than FAIL.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$ROOT" ]; then
  echo "FAIL  not inside a git repository — cd into the libsync checkout first"
  exit 1
fi
cd "$ROOT"

FAILURES=0
pass() { echo "PASS  $1"; }
warn() { echo "WARN  $1"; }
fail() { echo "FAIL  $1"; FAILURES=$((FAILURES + 1)); }

# 1. Submodule initialized and at the pinned commit?
SUB_STATUS="$(git submodule status sdks/qobuz_api_client 2>/dev/null || true)"
case "$SUB_STATUS" in
  -*) fail "SDK submodule NOT initialized — run: make deps" ;;
  +*) warn "SDK submodule checked out at a DIFFERENT commit than the repo pin: ${SUB_STATUS:1:12}..." ;;
  " "*|U*)
    pass "SDK submodule initialized at pinned commit ${SUB_STATUS:1:9}..."
    ;;
  *) fail "SDK submodule status unreadable: '$SUB_STATUS'" ;;
esac

DIRTY_COUNT="$(git -C sdks/qobuz_api_client status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
if [ "${DIRTY_COUNT:-0}" -gt 0 ]; then
  warn "SDK submodule working tree is DIRTY ($DIRTY_COUNT modified files) — 'make deps' / 'git submodule update' may clobber local edits"
else
  pass "SDK submodule working tree is clean"
fi

# 2. SDKs importable in the poetry venv?
if poetry run python -c "import qobuz" >/dev/null 2>&1; then
  pass "qobuz SDK importable in poetry venv"
else
  fail "qobuz SDK NOT importable — run: make deps"
fi
if poetry run python -c "import tidal" >/dev/null 2>&1; then
  pass "tidal SDK importable in poetry venv"
else
  fail "tidal SDK NOT importable — run: make deps"
fi
if poetry run python -c "import websockets" >/dev/null 2>&1; then
  pass "websockets importable (needed by ws-probe.py)"
else
  fail "websockets NOT importable — run: poetry install"
fi

# 3. ffmpeg on PATH (conversion + some SDK operations)
if command -v ffmpeg >/dev/null 2>&1; then
  pass "ffmpeg on PATH ($(command -v ffmpeg))"
else
  warn "ffmpeg NOT on PATH — the Docker image installs it; audio conversion will fail locally without it"
fi

# 4. Python version vs CI pin (3.12)
PYVER="$(poetry run python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null)"
if [ -z "$PYVER" ]; then
  fail "poetry venv has no working python — run: poetry install"
elif [[ "$PYVER" == 3.12.* ]]; then
  pass "venv Python $PYVER matches CI pin (3.12)"
else
  warn "venv Python $PYVER differs from CI pin 3.12 (pyproject allows >=3.10 <4.0; tests may behave differently)"
fi

# 5. Node version vs CI pin (20)
if command -v node >/dev/null 2>&1; then
  NODEVER="$(node --version)"
  if [[ "$NODEVER" == v20.* ]]; then
    pass "node $NODEVER matches CI pin (20)"
  else
    warn "node $NODEVER differs from CI pin v20 — frontend build may diverge from CI"
  fi
else
  warn "node NOT on PATH — frontend cannot be built locally (make dev needs it; make dev-backend does not)"
fi

# 6. Built frontend present for local serving?
if [ -f backend/static/index.html ]; then
  pass "backend/static/index.html exists — 'make dev-backend' will serve the UI"
else
  warn "backend/static/ missing or empty — run 'make dev' (builds frontend) or expect API-only"
fi

# 7. App DB present? (informational — absent on a fresh clone)
DB="${STREAMRIP_DB_PATH:-data/streamrip.db}"
if [ -f "$DB" ]; then
  pass "app DB exists at $DB"
else
  warn "app DB not found at $DB — created on first backend start (fresh clone is fine)"
fi

echo
if [ "$FAILURES" -gt 0 ]; then
  echo "$FAILURES check(s) FAILED"
  exit 1
fi
echo "All hard checks passed (warnings above are informational)."
