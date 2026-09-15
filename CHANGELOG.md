# Changelog

All notable changes to Libsync are documented in this file. Release tags are annotated git tags; each section below mirrors the tag message for easy GitHub browsing.

## Unreleased

No changes yet.

---

## v0.0.7 — 2026-09-15

Reliability release for downloads, library reconciliation, authentication, and the web UI, with a non-root Docker runtime. Includes all changes since v0.0.6.

### Upgrade — Docker permissions required

**The container now runs as UID/GID `1000:1000`.** Existing bind mounts and previously created named volumes must be writable by that user before upgrading. Stop the app and back up its data directory (including `streamrip.db`, `downloads.db`, and `downloads-tidal.db`) first. Existing music files are not moved or renamed.

For the published-image Compose example, run these commands from the directory containing your Compose file. Substitute your actual host mount paths for `./data` and `./music`:

```bash
docker compose -f docker-compose.example.yml stop
sudo chown -R 1000:1000 ./data ./music
docker compose -f docker-compose.example.yml pull
docker compose -f docker-compose.example.yml up -d
```

Equivalent filesystem permissions or ACLs are also sufficient; only change ownership on the directories dedicated to this deployment. Custom download paths must also be writable by UID 1000.

If `/data` uses an existing named volume, migrate that volume's ownership separately while the app is stopped. Replace `YOUR_DATA_VOLUME` with the actual name shown by `docker volume ls` (Compose may prefix it with the project name):

```bash
docker run --rm --user 0 --entrypoint chown \
  -v YOUR_DATA_VOLUME:/data \
  ghcr.io/arthursoares/libsync:0.0.7 -R 1000:1000 /data
```

New empty named volumes inherit the correct ownership automatically. After startup, verify the container becomes `healthy` and that downloads can write to the music mount. The stable Docker tags are `ghcr.io/arthursoares/libsync:0.0.7` and `:latest`.

Manual mark/unmark and scan reconciliation now require a connected source and a complete online track catalog. Unset or explicitly empty naming formats use the canonical Settings defaults; stored custom formats are preserved. Existing database names, environment variables, and sentinel filenames remain compatible. The internal Python package version (`3.0.0`) and frontend package version (`0.0.1`) remain independent of Libsync's release tags.

### Added

- **Sync Download Selected.** The New in Library selection now queues the chosen albums, and disconnected sources and sync failures have explicit states. ([#63](https://github.com/arthursoares/libsync/pull/63))
- **Container health check.** Docker probes `/api/health` every 30 seconds using Python's standard library; the loopback probe ignores outbound proxy settings. ([#62](https://github.com/arthursoares/libsync/pull/62))

### Changed

- **Non-root Docker runtime.** The app runs as `libsync` (UID/GID 1000), with writable default data and music directories. See the required upgrade steps above. ([#62](https://github.com/arthursoares/libsync/pull/62))

### Fixed

- **Tidal HiRes busy retry.** A busy response preserves the authorization handle and pasted redirect URL, allowing an explicit retry without restarting login. ([#93](https://github.com/arthursoares/libsync/pull/93))
- **Scan polling lifecycle.** Scan status requests run sequentially and stop on close, navigation, or error. Missing jobs offer a fresh scan; connection failures offer an explicit status retry without repeated polling errors. ([#88](https://github.com/arthursoares/libsync/pull/88))
- **Completion detail refresh.** Queue UUID completion events now resolve the album's source and catalog ID before refreshing matching open details; late progress cannot restore completed queue items. ([#87](https://github.com/arthursoares/libsync/pull/87))
- **Cancellation feedback.** Individual and bulk cancellation now reload the canonical queue immediately, updating active counts without WebSocket traffic and ignoring late progress for cancelled items. ([#86](https://github.com/arthursoares/libsync/pull/86))
- **Search pagination.** Load More appends the next page without triggering a page-one reload; changing services still reruns the active query from page one. ([#84](https://github.com/arthursoares/libsync/pull/84))
- **Sync selection effect loop.** Initial selection notifications no longer track parent state; deselection stays intact and replacement sync results reseed once. ([#83](https://github.com/arthursoares/libsync/pull/83))
- **Source-scoped selections and details.** Switching services clears Library and Search selections and detail panels; album actions use the album's source, and late detail responses cannot replace a newer selection. ([#82](https://github.com/arthursoares/libsync/pull/82))
- **Detail refresh during status updates.** Same-album status events no longer discard pending track details or suppress mark/unmark refreshes; newer status is preserved when a pending detail response arrives. ([#82](https://github.com/arthursoares/libsync/pull/82))
- **Settings load protection.** Save stays disabled until configuration loads successfully, with a visible retry action on failure and no partial form hydration during auth checks. ([#80](https://github.com/arthursoares/libsync/pull/80))
- **Frontend development API proxy.** Vite now forwards same-origin HTTP and WebSocket API traffic to the backend on port 8080; production behavior is unchanged. ([#79](https://github.com/arthursoares/libsync/pull/79))
- **Tidal authentication transitions.** Completing device-code authentication now replaces a previously stored PKCE auth method, keeping persisted credentials and client initialization consistent. ([#78](https://github.com/arthursoares/libsync/pull/78))
- **Reliable mark/unmark reconciliation.** Downloads now cache the complete authoritative track catalog before starting, while manual mark/unmark and fuzzy auto-mark refresh it online before changing album, sentinel, or dedup state. Mark/unmark now requires a connected source and fails clearly if the catalog is unavailable or incomplete. ([#81](https://github.com/arthursoares/libsync/pull/81))
- **Atomic album/dedup updates.** Manual and scan mark/unmark now update album state and the per-source dedup database in one attached SQLite transaction, rolling both back on ordinary statement or lock failures. Best-effort sentinel writes and removals happen only after that mandatory commit. ([#85](https://github.com/arthursoares/libsync/pull/85))
- **Safe legacy sentinel reconciliation.** The downloads scan now discovers Qobuz and Tidal sentinels itself, requires a complete online catalog and matching local audio set, records the actual folder, and uses the same atomic album/dedup update as manual and fuzzy reconciliation. Malformed, partial, offline, or unsafe folders are reported without aborting healthy entries. ([#89](https://github.com/arthursoares/libsync/pull/89))
- **Owned shutdown drainage.** Shutdown now rejects new background work, drains the current album without advancing queued downloads, interrupts and records active syncs, cooperatively stops scans after cancellation-safe off-loop writes, waits for progress events, and only then closes current SDK clients. Repeated caller cancellation is propagated after the retained cleanup operation finishes. ([#90](https://github.com/arthursoares/libsync/pull/90))
- **Transactional credential reloads.** Qobuz and Tidal credential changes now build, open, and validate replacement SDK clients before atomically persisting credentials and publishing them through the shared client map. Active source work returns HTTP 409 without being interrupted; failed or cancelled activation preserves the previous credentials and exact client objects. ([#91](https://github.com/arthursoares/libsync/pull/91))
- **Consistent naming defaults.** Unset folder and track naming formats now use the same canonical defaults in Settings, the config API, and both source downloaders; explicitly stored custom formats remain unchanged. ([#92](https://github.com/arthursoares/libsync/pull/92))
- **Download integrity and retries.** Downloads preserve album artwork and metadata, isolate metadata failures within a batch, persist failed status across restarts, and retain track file metadata during status updates. The `all` library filter reports the correct total. ([#60](https://github.com/arthursoares/libsync/pull/60))
- **Accurate download completion and progress.** Completed downloads record the album folder; below-threshold downloads remove misleading sentinels without discarding successful-track dedup records. Concurrent track byte progress is aggregated, and finished queue/scan history is bounded in memory. ([#65](https://github.com/arthursoares/libsync/pull/65))
- **Responsive library sync and restart recovery.** Album writes are batched off the event loop, and startup clears orphaned queued/downloading states without automatically re-enqueueing them. ([#64](https://github.com/arthursoares/libsync/pull/64))
- **Safer fuzzy scans.** Track-count mismatches go to review, folder walking runs off the event loop, and per-folder failures are reported without discarding healthy results. ([#56](https://github.com/arthursoares/libsync/pull/56))
- **API lifecycle and validation.** Pagination is bounded, sentinel settings parse consistently, album status events reach WebSocket consumers, and client shutdown cleanup is explicit. ([#57](https://github.com/arthursoares/libsync/pull/57))
- **Clearer frontend feedback.** Enqueue actions show feedback, album details display errors, stale source/query responses are ignored, and the WebSocket connection store tracks actual connection state. ([#55](https://github.com/arthursoares/libsync/pull/55), [#63](https://github.com/arthursoares/libsync/pull/63))

### Internal

- Updated the yanked aiohttp dependency, consolidated development dependencies, and capped Python support at `>=3.10,<3.14`. ([#58](https://github.com/arthursoares/libsync/pull/58))
- Pinned Ruff to match CI, excluded non-product files from lint/build inputs, fixed branch build triggers, and removed the unused E2E job. ([#59](https://github.com/arthursoares/libsync/pull/59))
- Expanded backend, frontend, WebSocket, authentication, and lifecycle regression coverage; refreshed maintainer guidance. ([#61](https://github.com/arthursoares/libsync/pull/61), [#16](https://github.com/arthursoares/libsync/pull/16))

### Validation and known limitations

- Combined release validation: 371 backend tests, 48 frontend tests, Ruff 0.16.5 lint/format, Svelte checks, and the production build passed.
- Docker startup, UID/GID 1000 writes, HTTP health/frontend, proxy-independent health checks, and an existing-volume ownership migration passed using disposable volumes.
- Live credentialed Qobuz/Tidal downloads were not exercised for this release.
- Existing high-severity dependency alerts remain in frontend development tooling (nanoid, PostCSS, and Vite); these dependencies are not installed in the final Python runtime image. Track them in [Dependabot](https://github.com/arthursoares/libsync/security/dependabot).

### Contributors

Thanks to [Arthur Soares (@arthursoares)](https://github.com/arthursoares) for this release, including the final naming and container fixes ([#92](https://github.com/arthursoares/libsync/pull/92), [#62](https://github.com/arthursoares/libsync/pull/62)). All contributing PRs are linked above.

### Commits since v0.0.6

https://github.com/arthursoares/libsync/compare/v0.0.6...v0.0.7

---

## v0.0.6 — 2026-04-27

Small UX polish release on top of v0.0.5.1.

### Added

- **Per-page document titles.** Every route (`library`, `search`, `playlists`, `sync`, `settings`, `downloads`) now sets its own `<title>` via `<svelte:head>`, so browser tabs / bookmarks / history land on something meaningful instead of the bare URL path.

### Changed

- **UI rebrand to "Libsync".** Replaces the last user-facing `streamrip` strings: app `<title>` default, sidebar header (also drops the stale "v3.0.0 — library manager" subtitle inherited from the upstream Python package version, in favor of "Qobuz & Tidal library manager"), Settings *Download Path* placeholder. Internals that retain the legacy `streamrip` name (Python module, `STREAMRIP_DB_PATH`, `streamrip.db`, `.streamrip.json`, `logging.getLogger("streamrip")`) stay as-is per the v1.0 plan in `CLAUDE.md`.

### Internal

- SDK submodule pin bumped from the v0.0.5.1 feature-branch SHA to the merged `main` SHA on `arthursoares/qobuz_tidal_api_client`. Same code; pin now points at a published, non-feature-branch commit.

### Commits since v0.0.5.1

https://github.com/arthursoares/libsync/compare/v0.0.5.1...v0.0.6

---

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
