#!/usr/bin/env bash
# album-status-report.sh — album counts per source x download_status from
# the app DB, flagging albums stuck in 'queued'/'downloading' (a restart
# artifact: the download queue is in-memory only and nothing resets DB
# status on boot).
#
# Usage: album-status-report.sh [path/to/streamrip.db]
# Default DB: $STREAMRIP_DB_PATH, else data/streamrip.db at the repo root.
#
# Read-only.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
DB="${1:-${STREAMRIP_DB_PATH:-$ROOT/data/streamrip.db}}"

if [ ! -f "$DB" ]; then
  echo "ERROR: DB not found: $DB" >&2
  exit 1
fi

python3 - "$DB" <<'PYEOF'
import sqlite3
import sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print(f"{'SOURCE':<8} {'STATUS':<16} COUNT")
print("-" * 34)
rows = conn.execute(
    """SELECT source, download_status, COUNT(*) AS cnt
       FROM albums GROUP BY source, download_status
       ORDER BY source, download_status"""
).fetchall()
for r in rows:
    print(f"{r['source']:<8} {r['download_status']:<16} {r['cnt']}")
if not rows:
    print("(albums table is empty)")

stuck = conn.execute(
    """SELECT id, source, artist, title, download_status
       FROM albums WHERE download_status IN ('queued', 'downloading')
       ORDER BY id"""
).fetchall()
print()
if stuck:
    print(f"STUCK ROWS ({len(stuck)}) — status says in-flight but the in-memory")
    print("queue does not survive a backend restart; these are almost certainly")
    print("restart artifacts. Re-enqueue them, or reset status via the UI.")
    for r in stuck:
        print(f"  id={r['id']} [{r['source']}] {r['artist']} - {r['title']} ({r['download_status']})")
else:
    print("No albums stuck in 'queued'/'downloading'. Good.")

nofolder = conn.execute(
    """SELECT COUNT(*) FROM albums
       WHERE download_status = 'complete' AND local_folder_path IS NULL"""
).fetchone()[0]
if nofolder:
    print()
    print(f"NOTE: {nofolder} album(s) are 'complete' with NULL local_folder_path")
    print("(marked before schema v2, or reconciled via the legacy POST")
    print("/api/downloads/scan which never records the folder). check-sentinels.sh")
    print("cannot cross-check these against disk.")
conn.close()
PYEOF
