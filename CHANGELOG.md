# Changelog

All notable changes to Libsync are documented in this file. Release tags are annotated git tags; each section below mirrors the tag message for easy GitHub browsing.

## v0.0.5 — 2026-04-27

Bug-fix and feature release. Headlines: real Tidal HiRes Lossless via a new PKCE OAuth flow (the legacy device-code client was capped at 320 kbps AAC regardless of subscription), search→download metadata round-trip, and a retry button on failed downloads.

### Added

- **Tidal HiRes login.** New "Connect Tidal (HiRes)" button in Settings runs an Authorization Code + PKCE flow against Tidal's HiRes-capable client (`6BDSRdpK9hqEBTgU`). The legacy device-code button is preserved but renamed "(legacy, AAC only)" — it's an entitlement cap on the OAuth client itself, independent of subscription tier. Tokens are persisted with a `tidal_auth_method` marker so refresh dispatches to the matching helper.
- **DASH manifest + multi-segment download.** PKCE-issued tokens make Tidal return `application/dash+xml` manifests instead of the legacy single-URL JSON. The SDK now parses MPEG-DASH `SegmentTemplate` + `SegmentTimeline`, downloads init + N media segments sequentially, and concatenates them into a fragmented MP4. When `ffmpeg` is on PATH it gets remuxed (`-c:a copy`) into native FLAC so mutagen can tag it; otherwise the file is left as MP4-with-FLAC and a warning logs.
- **Retry button** on failed/cancelled rows in the Downloads page. Posts to `/api/downloads/queue` with `force=true` so the per-source dedup DB doesn't skip-mark partial downloads.
- **Search→download metadata round-trip.** Search results now forward their full title/artist/cover/track-count payload alongside `album_ids` when enqueueing. Backend prefers the supplied dict over re-fetching from the streaming service.

### Fixed

- **Search downloads showed `Album <id>` / Unknown.** `_fetch_album_metadata` used to silently fall back to a placeholder string when the SDK round-trip failed, then persist that placeholder to the DB where it stuck. Now: prefer caller-supplied metadata, fail loud when neither is available.
- **Folder label vs actual codec mismatch (Tidal).** Folders were tagged `[FLAC-…]` even when the real downloads were AAC m4a, because `_tidal_quality_fields` used the album's max-available tier instead of the actual download tier. Now computes `min(album_cap, user_request)`. Also: `HI_RES` (legacy MQA) correctly labels as `[FLAC-16-44.1]` since MQA is physically 16/44.1 with extra subbands.
- **Folder label sample-rate mismatch (Qobuz).** Same family of bug: a 24/192 album downloaded at CD quality got `[FLAC] [24B-192kHz]`. Now mirrors the requested tier (CD = 16/44.1, tier 3 = 24/96-cap, tier 4 = album max).
- **`Load More` disappeared after page 1 in Search.** When the Qobuz SDK returned `total=None`, the backend's `getattr(result, "total", default)` leaked `None` to the JSON response (`total: null`), the frontend's `?? items.length` fell back to page size, and `total > results.length` evaluated false. Replaced with an explicit None check.
- **Tidal silent-downgrade is now visible.** Manifest decode failures used to silently walk down the tier ladder; now a warning logs the requested quality + manifest mime type before fallback. Added a one-line `tidal manifest …` debug log on every successful manifest decode.

### SDK

- **HI_RES_LOSSLESS (tier 4)** added to `QUALITY_MAP`. Settings dropdown gains the new option. Until your Tidal subscription includes it, it falls back via the standard tier walk.
- **Tidal search envelope** is now defensively unwrapped — handles both `{items, totalNumberOfItems}` and `{albums: {items, …}}` shapes.

### Schema

- `AppConfig` gains `tidal_auth_method: str` (defaults to `"device_code"`; PKCE flow sets it to `"pkce"`). `DownloadRequest` gains an optional `albums: list[DownloadAlbumMetadata]` field.

### API additions

| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/tidal/pkce-start` | POST | Returns `auth_url` + `handle` for the PKCE flow |
| `/api/auth/tidal/pkce-complete` | POST | `{handle, redirect_url}` — exchange code for HiRes-capable token |

### Notes

- **ffmpeg is recommended for HiRes.** Without it, lossless downloads land as MP4-with-FLAC (`.flac` extension but mp4 magic bytes). Most players handle that fine; strict FLAC scanners (and our own mutagen tag pipeline) need real native FLAC.
- **Tidal subscription tier ≠ OAuth client tier.** Two separate caps. The new HiRes button uses a HiRes-capable client; users on TIDAL Free/Premium will still get capped at HIGH (AAC) by their subscription — but for HiFi/Individual+ accounts, real HiRes is now reachable.

### Commits since v0.0.4

https://github.com/arthursoares/libsync/compare/v0.0.4...v0.0.5

---

## v0.0.4 — 2026-04-19

Bugfix release on top of v0.0.3. Four issues found while testing the fuzzy-scan against a real 1,800-album library.

### Fixed

- **Scan panel stuck at `0 / ?`** — `GET /api/library/scan-fuzzy/{job_id}` now includes live `scanned` / `total` progress while the job is running. The scan's `event_bus.publish("scan_progress", …)` events already drove the WebSocket channel, but the REST polling endpoint was returning `{"status": "running"}` with no progress fields; the UI renders `{scanned ?? 0} / {total ?? '?'}` and sat at `0 / ?` for the whole scan. The POST handler now wraps the event bus so progress events also update the in-memory job registry.
- **Scan Review panel had no background** — `ScanReview.svelte` referenced CSS tokens that don't exist in `design-system/tokens.css` (`--surface`, `--fg`, `--muted`, `--shadow-color`); browsers resolved the unknown `var()`s to empty and the panel blended into the page. Rewired to the real tokens (`--canvas-raised`, `--text-primary`, `--text-secondary`, `--shadow-lg`) and added a `--border` left edge.
- **Library grid didn't refresh on Mark / Unmark** — the manual button and fuzzy-scan auto-matches flipped status in the DB and published `album_status_changed` over WebSocket, but nobody was listening in the frontend library store. Now patched in place so grid pills and the open detail panel update live.
- **Load More instantly overwritten by a page-1 refetch** — the library page's reload-on-filter `$effect` called `fetchAlbums()`, which read `currentPage` while building params. Svelte 5 `$effect` tracks reactive reads transitively, so `currentPage` became a dependency of the effect; bumping it via Load More re-triggered the effect, reset `currentPage` to 1, and overwrote page 2 with page 1. Fixed by wrapping the effect body in `untrack()` so only `source` / `sort` / `filter` count as dependencies.

### Commits since v0.0.3

https://github.com/arthursoares/libsync/compare/v0.0.3...v0.0.4

---

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
