# Library Scan — Fuzzy Matching + Manual Mark-as-Downloaded

## Motivation

Today's "Scan Folder" button (`POST /api/downloads/scan`, backend/api/downloads.py:50) only finds albums that have a `.streamrip.json` sentinel file — i.e., albums Libsync itself downloaded. Users who already own local FLAC/MP3 collections acquired before adopting Libsync can't reconcile that existing library: every favorited album shows as "not downloaded" even when the files are sitting in the download folder.

This feature closes the gap by (a) fuzzy-matching folders against the synced library and (b) giving the user a way to mark an album as downloaded from the UI without any file-system involvement.

## User Stories

- **New Libsync user with a 2TB existing collection.** Runs scan. Exact-artist + exact-album + matching bit-depth entries are marked done automatically. A review list surfaces the ambiguous cases (e.g. local 16-bit copy of an album the library has in 24-bit). A separate read-only list shows folders that don't match any library album.
- **User notices one album shows as "not downloaded" but the files exist.** Clicks "Mark as downloaded" on the album detail page. Status flips; the per-source dedup DB is updated so accidental later clicks won't re-download it.

## Scope / non-goals

- **In scope:** fuzzy scan, review UI, manual mark/unmark, sentinel write-back, dedup-DB population, one small schema migration.
- **Out of scope:** local-only albums without a library match (they go in the unmatched report only), track-level file-path recording, learning from user-confirmed ambiguous matches, reverse-looking up streaming-service IDs for orphan local folders.

## Shared primitive

One backend function in a new `backend/services/scan.py` (keeping `library.py` focused on sync/refresh concerns):

```python
def mark_album_downloaded(
    db: Database,
    album_id: int,
    local_folder_path: str | None = None,
    downloaded_at: str | None = None,
) -> None
```

Does:

1. `UPDATE albums SET download_status='complete', downloaded_at=?, local_folder_path=? WHERE id=?`
2. Populate the per-source dedup DB (`data/downloads.db` or `data/downloads-tidal.db`) with every track ID for this album so a later accidental download won't re-fetch. Resolves the dedup DB path via the same logic as `DownloadService._download_album` (`os.path.dirname(STREAMRIP_DB_PATH)`).
3. If `local_folder_path` was given and the folder exists but has no `.streamrip.json`, write one. Keep the existing sentinel shape the SDK uses (`source`, `album_id`, `title`, `artist`, `tracks_count`, `downloaded_at`). This keeps the existing sentinel-scan safe and idempotent.
4. Emit a WebSocket event so any UI views (Library, Downloads) refresh.

Used by both the scan confirm flow and the manual mark button. Idempotent — calling it twice is a no-op after the first.

Companion: `unmark_album_downloaded(db, album_id)` — flips status back to `not_downloaded`, clears `downloaded_at` and `local_folder_path`, removes the matching entries from the dedup DB, deletes the sentinel file if present. Small safety net.

## DB migration (schema v2)

Add three columns to `albums` (all nullable, backward-compatible):

- `bit_depth INTEGER`
- `sample_rate REAL`
- `local_folder_path TEXT`

Populate `bit_depth` / `sample_rate` during the next library sync by persisting `maximum_bit_depth` / `maximum_sampling_rate` (already computed at library.py:191). On migration, best-effort backfill from the existing `quality` string (e.g. `"FLAC 24/96kHz"` → `bit_depth=24`, `sample_rate=96`). Albums where parsing fails stay `NULL` and simply lose the bit-depth signal during matching.

Migration lives in `Database._init_schema` alongside the existing `SCHEMA_VERSION` check. Bumps `SCHEMA_VERSION` from 1 to 2.

## Fuzzy scan

### Endpoint

- `POST /api/library/scan-fuzzy` — starts a background job, returns `{"job_id": "..."}`.
- `GET /api/library/scan-fuzzy/{job_id}` — polls status + result.
- Progress events emitted over the existing WebSocket event bus: `scan_progress` (`{scanned, total}`), `scan_complete` (full result payload).

Running a job while another is in progress returns 409 — one scan at a time.

### Pipeline

For each immediate child of `downloads_path` that is a directory:

1. **Skip sentineled folders.** If `.streamrip.json` exists, the existing sentinel scan already handles it. We run that scan first (or reuse its result) before walking for fuzzy-matching.
2. **Extract metadata.** Read the first audio file in the folder with `mutagen` (already a transitive dep via the SDKs). Collect:
   - `albumartist` (fall back to `artist`)
   - `album`
   - `bits_per_sample` (via `mutagen.File(..., easy=False).info.bits_per_sample` for FLAC/WAV; None for lossy)
   - `sample_rate`
   - track count from `len([f for f in folder if audio])`
   - If tags are missing, parse the folder name with a small regex: `Artist - Album`, `Artist/Album` (folder split), or fall back to just album = folder basename.
3. **Normalize** artist + album for comparison:
   - `.casefold()`
   - Strip leading `"The "` (common reshuffle between tags and folders)
   - NFKD Unicode normalize + strip combining marks (diacritics)
   - Strip trailing parenthetical/bracketed suffixes: `(Deluxe)`, `(Remastered 2021)`, `[Explicit]`, `[Bonus Track Version]`
   - Collapse whitespace
4. **Match.** SQL: `SELECT * FROM albums WHERE normalize(artist)=? AND normalize(title)=?`. Since SQLite doesn't have our normalizer, we do it in Python: fetch `SELECT id, artist, title, bit_depth, source FROM albums`, normalize on read, keep a dict keyed by `(norm_artist, norm_title)` for O(1) lookup per folder. Libraries of ~10k albums fit easily in memory.
5. **Classify** each folder:
   - **Auto-match.** Exactly one candidate with matching normalized artist + album AND (bit_depth matches OR bit_depth unknown on either side). Immediately call `mark_album_downloaded(album_id, local_folder_path=folder)`.
   - **Needs review.** Multiple candidates, or bit_depth mismatch, or partial match (e.g. album matched but artist was missing from the folder). Include folder + candidate list + reason per candidate.
   - **Unmatched.** No normalized key hits. Include folder path only; no action offered.

### Response shape

```json
{
  "status": "complete",
  "scanned": 812,
  "sentinel_reconciled": 120,
  "auto_matched": [
    {"album_id": 42, "folder": "/music/Radiohead/In Rainbows", "reason": "exact"}
  ],
  "review": [
    {
      "folder": "/music/Beatles/Abbey Road",
      "local_bit_depth": 16,
      "candidates": [
        {"album_id": 17, "score": 0.87, "artist": "The Beatles", "title": "Abbey Road",
         "reason": "bit_depth_mismatch: local=16 library=24"}
      ]
    }
  ],
  "unmatched": ["/music/Some Band/Weird Album"]
}
```

## API: mark / unmark

- `POST /api/library/albums/{id}/mark-downloaded` — body `{"local_folder_path": "..."}` (optional). Calls `mark_album_downloaded`. Returns the updated album row.
- `POST /api/library/albums/{id}/unmark-downloaded` — calls `unmark_album_downloaded`. Returns the updated album row.

Both endpoints are used by the scan confirm flow AND by the manual button on the album detail page — same primitive either way.

## Frontend

### Settings — Scan Folder flow

The existing Settings "Scan Folder" button changes behavior: instead of showing a one-line text result, it opens a right-side slide-over panel (matches the design system's preference for overlays over modals; resolves to a full route if it becomes too cramped during implementation). Stages:

1. **Scanning.** Progress bar + `Scanned X / Y` based on `scan_progress` WS events.
2. **Results.** Three stacked sections, each collapsible:
   - **Auto-matched (N)** — read-only list, one line per album (`Artist — Title → folder`). Default collapsed.
   - **Needs review (M)** — expanded by default. Each entry is a card: folder on the left, candidate(s) on the right with score + reason. Per-row **Confirm** and **Skip** buttons. Header has **Confirm all** (applies every ambiguous match) for power users.
   - **Unmatched (K)** — plain list of folder paths, default collapsed. No action offered. Header has a **Copy to clipboard** button for export.

Confirming a review entry calls `POST /api/library/albums/{id}/mark-downloaded` with the folder path.

### Album detail page

`frontend/src/lib/components/AlbumDetail.svelte` gets a new button near the existing download controls:

- If `download_status !== 'complete'`: **Mark as downloaded** — calls `/mark-downloaded` with no path.
- If `download_status === 'complete'`: **Unmark** (secondary/subtle styling) — calls `/unmark-downloaded`.

No folder picker. Users who want to associate a specific folder use scan; users who just want to flip status use the button.

## Testing

Backend unit tests (`tests/`):

- `test_scan_normalize.py` — the normalizer function (The-prefix stripping, diacritics, parenthetical suffixes, whitespace, casefold).
- `test_scan_matcher.py` — matcher with fixture DB rows: exact / bit-depth-mismatch / multiple-candidates / no-match / missing-tags / folder-name-fallback.
- `test_mark_downloaded.py` — the shared primitive:
  - Status flip + `downloaded_at` stamp + `local_folder_path` stored.
  - Dedup DB entries inserted for each track ID.
  - Sentinel written when folder given, skipped when not.
  - Idempotent on repeat calls.
  - `unmark` reverses all of the above.
- `test_api_library.py` (extend existing) — mark/unmark endpoints happy path + 404 + 409 during active scan.

Frontend tests (`frontend/tests/`):

- Logic test for the review card's confirm handler (ensures it hits the right endpoint with the right body).

## Risks / open questions

- **Mutagen for many lossy/edge formats.** For `.m4a`, `.opus`, `.ape`, `.dsf`, bit-depth extraction may be flaky. Degrade gracefully: unknown bit_depth → still allow auto-match when the library album also has unknown bit_depth, otherwise push to review.
- **Scan time on large collections.** 10k albums × tag read = seconds, not minutes. Async-iterate folders and read tags off the event loop via `asyncio.to_thread` if needed. The WebSocket progress events keep the UI honest.
- **Normalization false positives.** Stripping parentheses can collapse distinct releases ("Greatest Hits" vs "Greatest Hits (Deluxe)"). Mitigation: when normalized keys collide on the library side, all candidates go to review, never auto-match.
- **Sentinel write permission.** Read-only mounts (e.g. NFS/SMB shares of an existing library) can't accept the sentinel write. The primitive must catch `OSError` / `PermissionError`, log a warning, and continue — the DB record is the source of truth, the sentinel is an optimization. A config toggle `scan_sentinel_write_enabled` (default `true`) lets users disable the write attempt up front when they know the mount is read-only, to avoid log noise across thousands of folders.

## Deliverable summary

1. `backend/models/database.py` — schema v2 migration, new columns populated during sync.
2. `backend/services/scan.py` (new) — normalizer, matcher, `mark_album_downloaded`, `unmark_album_downloaded`, the scan job.
3. `backend/api/library.py` — new `/scan-fuzzy` + `/albums/{id}/mark-downloaded` + `/albums/{id}/unmark-downloaded` routes.
4. `frontend/src/routes/settings/+page.svelte` — Scan Folder button opens the new review panel.
5. `frontend/src/lib/components/ScanReview.svelte` (new) — the three-section review panel.
6. `frontend/src/lib/components/AlbumDetail.svelte` — mark/unmark button.
7. Tests as listed above.
