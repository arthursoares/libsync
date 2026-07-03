#!/usr/bin/env bash
# dump-config.sh — print every row of the libsync config table with
# credential values redacted (key, length, last 4 chars only).
#
# Usage: dump-config.sh [path/to/streamrip.db]
# Default DB: $STREAMRIP_DB_PATH, else data/streamrip.db relative to repo root.
#
# Read-only: opens the DB in SQLite read-only mode. Never prints full
# token/secret values.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DB="${1:-${STREAMRIP_DB_PATH:-$ROOT/data/streamrip.db}}"

if [ ! -f "$DB" ]; then
  echo "ERROR: DB not found: $DB" >&2
  echo "Pass the path explicitly: dump-config.sh /data/streamrip.db" >&2
  exit 1
fi

python3 - "$DB" <<'PYEOF'
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT key, value, updated_at FROM config ORDER BY key"
).fetchall()
conn.close()

if not rows:
    print("config table is empty (fresh install — no settings saved yet)")
    sys.exit(0)

SECRET_MARKERS = ("token", "secret")

print(f"{'KEY':<28} {'VALUE':<52} UPDATED_AT")
print("-" * 100)
for key, value, updated_at in rows:
    is_secret = any(m in key.lower() for m in SECRET_MARKERS)
    # tidal_token_expiry is a unix timestamp, not a credential — show it,
    # since spotting a stale expiry is a primary use of this script.
    if key.endswith("_expiry"):
        is_secret = False
    if is_secret:
        tail = value[-4:] if len(value) >= 8 else "****"
        shown = f"REDACTED(len={len(value)}, ...{tail})"
    else:
        # repr() so 'True' vs 'true' and trailing whitespace are visible
        shown = repr(value)
        if len(shown) > 52:
            shown = shown[:49] + "..."
    print(f"{key:<28} {shown:<52} {updated_at}")

# Staleness hint for the Tidal token expiry timestamp
import time
expiry = dict((k, v) for k, v, _ in rows).get("tidal_token_expiry")
if expiry:
    try:
        delta = float(expiry) - time.time()
        if delta < 0:
            print(f"\nNOTE: tidal_token_expiry is {abs(delta) / 86400:.1f} days in the PAST.")
            print("This is EXPECTED: the SDK refreshes tokens in memory but nothing")
            print("persists the refreshed expiry back to the config DB. It is only a")
            print("problem if the stored tidal_refresh_token itself stops working.")
    except ValueError:
        print(f"\nNOTE: tidal_token_expiry is not a number: {expiry!r}")
PYEOF
