#!/usr/bin/env bash
# check-dedup.sh — report row counts and sample track IDs from the
# per-source dedup databases (downloads.db = Qobuz, downloads-tidal.db
# = Tidal) that make re-downloads skip already-downloaded tracks.
#
# Usage: check-dedup.sh [dedup-db-directory]
# Default dir: dirname of $STREAMRIP_DB_PATH, else data/ at the repo root
# (the same derivation the backend uses: dedup DBs live NEXT TO the app
# DB, not under the downloads path).
#
# Read-only.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
if [ -n "${1:-}" ]; then
  DIR="$1"
elif [ -n "${STREAMRIP_DB_PATH:-}" ]; then
  DIR="$(dirname "$STREAMRIP_DB_PATH")"
else
  DIR="$ROOT/data"
fi

echo "Dedup DB directory: $DIR"
echo

python3 - "$DIR" <<'PYEOF'
import os
import sqlite3
import sys

dedup_dir = sys.argv[1]

for source, fname in (("qobuz", "downloads.db"), ("tidal", "downloads-tidal.db")):
    path = os.path.join(dedup_dir, fname)
    print(f"[{source}] {path}")
    if not os.path.exists(path):
        print("  MISSING — created lazily on first download/mark for this source;")
        print("  absence is normal if this source has never completed a download.")
        print()
        continue
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "downloads" not in tables:
            print(f"  UNEXPECTED SCHEMA — tables: {sorted(tables)}")
            print()
            continue
        count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        samples = [
            r[0]
            for r in conn.execute("SELECT id FROM downloads LIMIT 5")
        ]
        print(f"  {count} track IDs recorded (each will be SKIPPED on re-download unless force=true)")
        if samples:
            print(f"  sample IDs: {', '.join(str(s) for s in samples)}")
    finally:
        conn.close()
    print()
PYEOF
