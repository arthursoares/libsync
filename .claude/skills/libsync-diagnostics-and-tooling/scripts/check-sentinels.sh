#!/usr/bin/env bash
# check-sentinels.sh — find every .streamrip.json sentinel under a music
# root, validate its JSON payload, and cross-check against the app DB:
#   - sentinel on disk but album NOT marked 'complete' in the DB
#   - album 'complete' in the DB but no sentinel found on disk
#
# Usage: check-sentinels.sh [music-root] [path/to/streamrip.db]
# Defaults: music-root = $STREAMRIP_DOWNLOADS_PATH else /music
#           DB         = $STREAMRIP_DB_PATH else data/streamrip.db at repo root
#
# Read-only. Never prints credentials (sentinels contain none).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
MUSIC="${1:-${STREAMRIP_DOWNLOADS_PATH:-/music}}"
DB="${2:-${STREAMRIP_DB_PATH:-$ROOT/data/streamrip.db}}"

if [ ! -d "$MUSIC" ]; then
  echo "ERROR: music root not found: $MUSIC" >&2
  echo "Usage: check-sentinels.sh <music-root> [db-path]" >&2
  exit 1
fi
if [ ! -f "$DB" ]; then
  echo "ERROR: DB not found: $DB" >&2
  exit 1
fi

python3 - "$MUSIC" "$DB" <<'PYEOF'
import json
import os
import sqlite3
import sys

music_root, db_path = sys.argv[1], sys.argv[2]

# Expected payload keys — mirrors _sentinel_payload in backend/services/scan.py
EXPECTED_KEYS = {"source", "album_id", "title", "artist", "tracks_count", "downloaded_at"}

sentinels = {}  # (source, album_id) -> folder
invalid = []
for dirpath, dirnames, filenames in os.walk(music_root):
    # Skip symlinked dirs, same as the backend scanner
    dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
    if ".streamrip.json" not in filenames:
        continue
    path = os.path.join(dirpath, ".streamrip.json")
    try:
        with open(path) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        invalid.append((path, f"unreadable/bad JSON: {e}"))
        continue
    missing = EXPECTED_KEYS - set(payload)
    if missing:
        invalid.append((path, f"missing keys: {sorted(missing)}"))
    src, aid = payload.get("source"), payload.get("album_id")
    if src and aid:
        sentinels[(str(src), str(aid))] = dirpath

conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
albums = conn.execute(
    "SELECT id, source, source_album_id, artist, title, download_status, local_folder_path FROM albums"
).fetchall()
conn.close()

by_key = {(a["source"], str(a["source_album_id"])): a for a in albums}

print(f"Scanned: {music_root}")
print(f"Sentinels found: {len(sentinels)} valid, {len(invalid)} invalid")
print()

if invalid:
    print("INVALID SENTINELS (bad JSON or wrong payload shape):")
    for path, reason in invalid:
        print(f"  {path}: {reason}")
    print()

sentinel_not_marked = []
for key, folder in sorted(sentinels.items()):
    album = by_key.get(key)
    if album is None:
        sentinel_not_marked.append((key, folder, "album not in library DB at all"))
    elif album["download_status"] != "complete":
        sentinel_not_marked.append(
            (key, folder, f"DB status is {album['download_status']!r}")
        )

if sentinel_not_marked:
    print(f"SENTINEL-BUT-NOT-MARKED ({len(sentinel_not_marked)}):")
    print("  (folder claims downloaded, DB disagrees — run the fuzzy scan,")
    print("   POST /api/library/scan-fuzzy, to reconcile)")
    for (src, aid), folder, reason in sentinel_not_marked:
        print(f"  [{src}] album_id={aid} at {folder} — {reason}")
    print()

marked_no_sentinel = []
for a in albums:
    if a["download_status"] != "complete":
        continue
    if (a["source"], str(a["source_album_id"])) in sentinels:
        continue
    marked_no_sentinel.append(a)

if marked_no_sentinel:
    print(f"MARKED-BUT-NO-SENTINEL ({len(marked_no_sentinel)}):")
    print("  (DB says complete, no sentinel under this music root — folder was")
    print("   moved/deleted, sentinel writes were disabled, or the album was")
    print("   marked with no local_folder_path)")
    for a in marked_no_sentinel:
        folder = a["local_folder_path"] or "(no folder recorded)"
        print(f"  [{a['source']}] id={a['id']} {a['artist']} - {a['title']} — {folder}")
    print()

if not sentinel_not_marked and not marked_no_sentinel and not invalid:
    print("Disk and DB agree. No mismatches.")
PYEOF
