# Changelog

All notable changes to Libsync are documented in this file. Release tags are annotated git tags; each section below mirrors the tag message for easy GitHub browsing.

## v0.0.3 — 2026-04-19

### Features

- **Library fuzzy-scan.** The Settings *Scan Folder* action now fuzzy-matches pre-existing `Artist/Album/` local collections against the synced library. Exact matches auto-mark complete; ambiguous cases (bit-depth mismatches, Qobuz + Tidal duplicates) land in a three-section review slide-over (auto-matched, needs review, unmatched). Spec: [`docs/superpowers/specs/2026-04-18-library-scan-fuzzy-match-design.md`](docs/superpowers/specs/2026-04-18-library-scan-fuzzy-match-design.md).
- **Manual *Mark as downloaded* / *Unmark* button** on the album detail panel — flips `download_status` without any filesystem involvement, for cases where the scan can't produce a clean match.
- **Tidal quality selector** now exposed in Settings. The backend already honored `tidal_quality`; only the UI was missing the input.

### Schema

- `albums` gains `bit_depth INTEGER`, `sample_rate REAL`, `local_folder_path TEXT` (schema v2). Existing v1 DBs migrate on first open. `bit_depth` / `sample_rate` are best-effort regex-backfilled from the legacy `quality` string (e.g. `"FLAC 24/96kHz"` → `bit_depth=24, sample_rate=96.0`). Rows without a parseable quality string keep `NULL`s — the matcher treats unknown bit-depth as compatible.

### Configuration

- New `scan_sentinel_write_enabled` toggle. Disable when pointing the scan at a read-only NFS/SMB mount of an existing library — the DB is still updated, sentinel writes are just skipped.

### Security

- `POST /api/library/albums/{id}/mark-downloaded` resolves `local_folder_path` and rejects (400) any path that isn't inside the configured downloads root.
- `_find_album_folders` skips symlinked children so a symlink placed inside `downloads_path` can't escape it.

### Lint / CI

- All 26 pre-existing `ruff check` errors cleaned up across the repo and `ruff format` applied everywhere. Both ruff steps in CI are green for the first time.

### API additions

| Endpoint | Method | Description |
|---|---|---|
| `/api/library/scan-fuzzy` | POST | Start a background fuzzy-scan job. Returns `{job_id}`. 409 while another scan is running. |
| `/api/library/scan-fuzzy/{job_id}` | GET | Poll job status. Complete shape: `{status, scanned, sentinel_skipped, skipped_dirs, auto_matched, review, unmatched}`. |
| `/api/library/albums/{id}/mark-downloaded` | POST | `{local_folder_path?}` — flip status, populate dedup DB, optional sentinel write. |
| `/api/library/albums/{id}/unmark-downloaded` | POST | Reverse mark-downloaded. |

New WebSocket events: `scan_progress`, `scan_complete`, `album_status_changed`.

### Commits since v0.0.2

https://github.com/arthursoares/libsync/compare/v0.0.2...v0.0.3

---

## v0.0.2 — 2026-04-14

Rebrand to Libsync, Codex fixes, SDK rename.

- Repo renamed: `arthursoares/streamrip` → `arthursoares/libsync`.
- New documentation, new GHCR image path (`ghcr.io/arthursoares/libsync`).
- Internals (env vars, SQLite filename, `.streamrip.json` sentinel, Python module name) deliberately retain the legacy `streamrip` prefix for compatibility — full internal rename queued for v1.0.
- **P1 path traversal** in SPA static-file route — `os.path.realpath` containment check (`backend/main.py`).
- **P1 auto-sync re-evaluation** after credential hot-reload — fixes a class of bugs where the auto-sync loop wouldn't pick up newly authenticated sources without a restart.
- Qobuz/Tidal SDK submodule rename (`qobuz_api_client` → `qobuz_tidal_api_client`).

## v0.0.1 — 2026-04-14

Initial release — web UI rewrite. Detached from `nathom/streamrip` fork. First release of the standalone FastAPI + SvelteKit project. The upstream CLI/TUI/media-pipeline is not part of this project; see [Acknowledgements](README.md#acknowledgements).
