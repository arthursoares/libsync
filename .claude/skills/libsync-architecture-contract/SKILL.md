---
name: libsync-architecture-contract
description: Use when changing libsync backend services, routers, event wiring, or SDK integration; when adding a new source, WebSocket event, config key, or a new writer of album download_status; when a WS event never reaches the UI, albums stay stuck in queued/downloading after a restart, dedup skips downloads unexpectedly, or pagination total reads 0; or when reviewing a diff that touches mark-downloaded, client hot-reload, dedup DB placement, or the static SPA route.
---

# libsync Architecture Contract

This skill states the load-bearing design decisions (with WHY and WHERE), the invariants your change must not break, and the known-weak points that are open on purpose. All facts were verified against the repo as of 2026-07-03 (v0.0.6). Line numbers drift; anchors are given as `file (function)` wherever possible.

Verification hygiene: when you re-check any claim below with grep, scope the search to `backend/` or `frontend/`. Repo-root greps also hit `.claude/worktrees/agent-*/` (full copies of deleted CLI-era code), `node_modules`, `.svelte-kit`, and `sdks/`, and will mislead you.

Terminology used throughout:

| Term | Meaning here |
|---|---|
| Facade | The SDK client object exposing `client.catalog` / `client.favorites` / `client.streaming` sub-APIs. |
| Dedup DB | A tiny per-source SQLite file with one table `downloads(id TEXT PRIMARY KEY)` listing already-downloaded track IDs, so re-downloads are skipped. |
| Sentinel | The `.streamrip.json` file written into an album folder marking it as downloaded; payload holds source, album_id, title, artist, tracks_count, downloaded_at. |
| EventBus | The in-process async pub/sub in `backend/services/event_bus.py`. Not a message queue; handlers run in the publisher's task. |
| Editable install | `pip install -e <path>`: Python imports run directly from the checked-out source, so local edits take effect without reinstalling. |

## 1. DECISIONS

Each row is a deliberate choice. Do not "fix" these; if you believe one must change, open a PR and follow the gates in `libsync-change-control`.

| # | Decision | Why | Where |
|---|---|---|---|
| D1 | Single-factory FastAPI app. `create_app(db_path)` builds the DB, EventBus, SDK clients, and all three services, then attaches them to `app.state` (`db`, `event_bus`, `library_service`, `download_service`, `sync_service`, `_clients_ref`, `scan_jobs`, `active_scan_job`). No dependency-injection framework. | Tests construct isolated apps against temp DBs by calling the factory; routers stay trivial (`request.app.state.*`). | `backend/main.py (create_app)`; run via `uvicorn backend.main:create_app --factory` in `Makefile` and `docker/Dockerfile` |
| D2 | Source dispatch by string. `_download_album` reads `item["source"]` and imports `AlbumDownloader, DownloadConfig` from either the `qobuz` or `tidal` package. Both facades expose the same `catalog`/`favorites`/`streaming` shape, so most other code is source-agnostic via `hasattr(client, "catalog")`. | Adding a source means adding one facade and one dispatch branch, not touching every service. | `backend/services/download.py (_download_album)`; hasattr checks in `backend/services/library.py` and `download.py` |
| D3 | SDKs are a git submodule installed editable, never vendored. `make deps` runs `git submodule update --init --recursive` then `pip install -e` for both `sdks/qobuz_api_client/clients/python` (package `qobuz`) and `.../python/tidal` (package `tidal`). | An exact upstream commit is pinned; fixes flow by bumping the pin. Trap: because installs are editable, any uncommitted edits in the submodule working tree are live in the running backend even though the pin is unchanged. | `Makefile (deps target)`, `.gitmodules` |
| D4 | Per-source dedup DBs. Qobuz uses `downloads.db` (legacy name kept to preserve existing skip-state on disk); every other source uses `downloads-{source}.db` (today only `downloads-tidal.db`). | Track IDs are plain strings per service and could collide across services in a shared table. | `backend/services/download.py (_download_album)` and `backend/services/scan.py (_dedup_db_path)` |
| D5 | Per-source quality keys. The backend reads config key `{source}_quality` (`qobuz_quality` / `tidal_quality`), default 3, fresh from the config DB on every download. | Each SDK has its own tier scale; the numbers are not interchangeable between services. See `qobuz-tidal-domain-reference` for what the tiers mean. | `backend/services/download.py (_download_album)` |
| D6 | SDK progress callbacks bridge to WebSocket via the EventBus. `AlbumDownloader(on_track_start, on_track_progress, on_track_complete)` callbacks mutate the in-memory queue item, then fire-and-forget `event_bus.publish("download_progress", ...)`, throttled to one emit per 0.5 s. `create_app` subscribes a fixed tuple of event types to `ConnectionManager.broadcast`; the manager is a module-level singleton. | Keeps the SDKs UI-agnostic and keeps WebSocket fan-out in one place. | `backend/services/download.py (_download_album wiring)`, `backend/main.py (create_app, the for-loop over event types)`, `backend/api/websocket.py (ConnectionManager, manager)` |
| D7 | In-memory serial download queue. `DownloadService._queue` is a plain list; one `_process_queue` asyncio task handles items strictly one album at a time. Cancellation is cooperative soft-cancel: the SDK cannot be interrupted mid-album, so cancelling during a download only prevents the "complete" marking afterwards (see the comment in `_process_queue`). | One album at a time is the intended throughput model; the SDKs parallelize tracks internally. | `backend/services/download.py (enqueue, _process_queue, cancel)` |
| D8 | SQLite schema v2 with regex backfill. `SCHEMA_VERSION = 2`. The v1→v2 migration adds `bit_depth`, `sample_rate`, `local_folder_path` to `albums`, then backfills the first two by matching the legacy `quality` string against the regex `(\d+)\s*/\s*([\d.]+)\s*kHz`. Rows that do not match (for example "MP3 320") keep NULL, and the scan matcher treats NULL bit depth as compatible. | Existing v1 deployments upgrade in place on first open with no manual step. | `backend/models/database.py (SCHEMA_VERSION, _migrate_to_v2)` |
| D9 | The SPA is served by the backend from `backend/static/` via a catch-all `GET /{path:path}` that resolves the requested path with `os.path.realpath` and serves `index.html` for anything outside the static root or not a file. | One process serves API and UI; the realpath check blocks path traversal. `make build-local` copies `frontend/build` into `backend/static`; Docker does the same at image build. | `backend/main.py (serve_frontend, end of create_app)` |
| D10 | Frontend is a Svelte 5 runes SPA on adapter-static with plain stores. `frontend/src/routes/+layout.ts` sets `ssr = false; prerender = false`; `frontend/svelte.config.js` forces runes on for all non-node_modules files and uses `@sveltejs/adapter-static` with `fallback: 'index.html'`. Shared state is classic `writable` stores in `frontend/src/lib/stores/`, no runes-based global state. All styling flows from `frontend/src/lib/design-system/tokens.css` (zero border-radius, solid 0-blur shadows, dark default). | No server rendering to operate; the backend static route is the only server. The design-system rules are stated in CLAUDE.md and encoded in tokens.css. | `frontend/svelte.config.js`, `frontend/src/routes/+layout.ts`, `frontend/src/lib/stores/`, `frontend/src/lib/design-system/tokens.css` |

## 2. INVARIANTS

These must hold after your change. Each is stated with its exact true scope, which in two cases is narrower than project docs imply.

### I1. Download-state triple: use the primitives on the scan/manual-mark path

`mark_album_downloaded` and `unmark_album_downloaded` in `backend/services/scan.py` are the shared primitives that atomically-in-intent update the triple: (a) `albums.download_status` (via `db.set_album_download_state` / `clear_album_download_state`), (b) per-source dedup DB rows, (c) the `.streamrip.json` sentinel (best-effort, respects `scan_sentinel_write_enabled`).

Verified call sites (as of 2026-07-03): the scan auto-match path in `backend/services/scan.py (run_scan)` and the endpoints `POST /api/library/albums/{id}/mark-downloaded` and `.../unmark-downloaded` in `backend/api/library.py`.

State the scope honestly — CLAUDE.md's "the primitives are the only place" claim reads wider than it is. Three other writers exist and are legitimate:

1. `DownloadService` writes `albums.download_status` directly via `db.update_album_status` throughout the queue lifecycle in `backend/services/download.py` (enqueue → "queued", worker → "downloading"/"complete", cancel/failure → "not_downloaded").
2. The legacy `POST /api/downloads/scan` endpoint (`backend/api/downloads.py (scan_downloads)`) reconciles sentinels by calling `db.update_album_status` directly — it bypasses the primitives, never populates the dedup DB, and never sets `local_folder_path`.
3. The SDK downloaders themselves write dedup rows (`_mark_downloaded` in the SDK downloader, fed by `downloads_db_path`) and write sentinels during real downloads.

The rule for you: never write the triple ad hoc when marking or unmarking an album as downloaded outside the download pipeline. Route through the primitives. Do not add a fourth independent writer.

### I2. Config and credentials live only in the `config` table

All settings and all credentials (Qobuz token/app-id/secret override, Tidal tokens) are rows in the `config(key, value, updated_at)` table inside the main app DB, accessed only through `AppDatabase.get_config` / `set_config` / `get_all_config` (`backend/models/database.py`). There is no TOML, YAML, or dotenv config file. The one raw-SQL exception is `POST /api/config/reset`, which deletes `albums`/`tracks`/`sync_runs` via `db._connect()` but deliberately preserves the `config` table. See `libsync-config-and-flags` for the full key catalog.

### I3. DB config overrides environment variables

The resolution chain for the downloads root is: `db.get_config("downloads_path")` → `STREAMRIP_DOWNLOADS_PATH` env var → `/music`. This chain appears in `backend/main.py (create_app)`, `backend/services/download.py (_build_dl_config_kwargs)`, and `backend/api/library.py (_resolve_downloads_root)`. The `STREAMRIP_*` env vars are fallbacks, not overrides. Known exception (a weak point, see W10): the legacy `POST /api/downloads/scan` skips the env var and falls straight from DB config to `/music`.

### I4. Dedup DB directory derives from the app DB path, not the downloads path

The dedup DB files live in `dirname(STREAMRIP_DB_PATH)` (default `data/`). This derivation is duplicated in `backend/services/download.py (_download_album)` and `backend/api/library.py (_dedup_db_dir)`; the scan primitives take it as the `dedup_db_dir` parameter. If you relocate the app DB, the dedup DBs move with it; changing `downloads_path` does not move them. Keep any new code consistent with this rule.

### I5. Credential writes must flow through the API so clients hot-reload

`PATCH /api/config` and the auth endpoints (`backend/api/auth.py`) are the sanctioned credential writers. When the PATCH touches any of `{qobuz_token, qobuz_user_id, qobuz_app_id, qobuz_app_secret, tidal_access_token}`, `_reload_clients` (`backend/api/config.py`) runs: it closes the old SDK sessions, rebuilds clients via `_init_clients`, re-resolves the Qobuz signing secret, swaps the `clients` dict on all three services, and re-evaluates auto-sync. Calling `db.set_config("qobuz_token", ...)` directly leaves stale clients in memory until restart. If you add a credential key, add it to the `cred_keys` set in `backend/api/config.py (update_config)`.

### I6. The WS bridge tuple in `create_app` is the only path to the frontend

`create_app` subscribes exactly these event types to the WebSocket broadcast (verified 2026-07-03, `backend/main.py` lines 253–261): `download_progress`, `download_complete`, `download_failed`, `sync_started`, `sync_complete`, `library_updated`, `token_expired`. `EventBus.publish` iterates the subscriber list for the type and silently does nothing when it is empty — there is no error, no log, no dead-letter. If you introduce a new event that the UI must see, you MUST add it to this tuple, and add a frontend `onEvent` handler (`frontend/src/lib/stores/websocket.ts`). Test both ends; see W2 for existing casualties of forgetting this.

## 3. KNOWN-WEAK POINTS

All items below are real, verified against code on 2026-07-03 (v0.0.6), and have status: open. None is fixed by pretending it is a bug you just discovered — check `libsync-failure-archaeology` before re-investigating, and route any fix through the normal PR gates.

| # | Weak point | Detail and anchor |
|---|---|---|
| W1 | Zero API authentication | No route or the WebSocket requires any credential (grep for `Depends`, `HTTPBearer`, `Security`, `APIKey` in `backend/` returns nothing). `GET /api/config` returns `qobuz_token`, `qobuz_app_secret`, and `tidal_access_token` in plaintext (`backend/api/config.py (get_config)`, `backend/models/schemas.py (AppConfig)`). Deployment safety is network placement only (LAN or reverse proxy). Do not expose the port publicly. |
| W2 | Dead event wiring in both directions | `token_expired` is in the WS bridge tuple but nothing anywhere in `backend/` publishes it. Conversely `album_status_changed` (published in `backend/api/library.py` mark/unmark endpoints), `scan_progress`, and `scan_complete` (published in `backend/services/scan.py`) are not in the bridge tuple, so the EventBus drops them silently. The frontend listener for `album_status_changed` in `frontend/src/lib/stores/library.ts` can never fire. Scan progress reaches the UI only via REST polling of `GET /api/library/scan-fuzzy/{job_id}` — intentional per the comment in `backend/api/library.py (start_scan)`. |
| W3 | Queue lost on restart, statuses left stuck | The download queue is a plain in-memory list. A server restart forgets queued and in-flight items but leaves the corresponding albums stuck at `download_status` "queued" or "downloading" in the DB; nothing resets them at boot. Symptom: albums permanently show a spinner state after a restart until re-enqueued or manually unmarked. |
| W4 | Client hot-reload can kill an in-flight download | `_reload_clients` (`backend/api/config.py`) calls `__aexit__` on the old clients unconditionally before building new ones. A credential PATCH while a download is running closes the session under the downloader. |
| W5 | Dedup DBs opened without WAL or timeout | `_populate_dedup` / `_remove_from_dedup` in `backend/services/scan.py` use bare `sqlite3.connect(path)` — no `PRAGMA journal_mode=WAL`, no busy timeout — unlike the main `AppDatabase` (`_connect` sets WAL and a 30 s timeout). A concurrent download (SDK writing dedup rows) plus scan can surface `database is locked`. |
| W6 | Refreshed Tidal tokens are never persisted | The Tidal SDK refreshes tokens in memory on `__aenter__` and on 401, but `set_config("tidal_access_token", ...)` exists only in the auth endpoints (`backend/api/auth.py`). The stored access token and `tidal_token_expiry` go stale immediately after the first refresh; the system keeps working only because the stored refresh token stays valid. |
| W7 | `status='all'` pagination count bug | `AppDatabase.get_albums` skips the status filter when `status == "all"`, but `count_albums` applies the filter for any truthy status including the literal string "all" (`backend/models/database.py`). Result: `GET /api/library/{source}/albums?status=all` returns rows with `total = 0`. |
| W8 | `update_track_status` NULLs file metadata | `AppDatabase.update_track_status` unconditionally overwrites `file_path`, `format`, `bit_depth`, `sample_rate` with its parameters, and `DownloadService` calls it as `update_track_status(track_id, "complete")` (`backend/services/download.py (_update_track_statuses_from_result)`), wiping any previously stored per-track file metadata. |
| W9 | Two different default folder/track formats | The `AppConfig` schema defaults (`backend/models/schemas.py`): `folder_format = "{albumartist}/({year}) {title} [{container}-{bit_depth}-{sampling_rate}]"`, `track_format = "{tracknumber:02}. {artist} - {title}{explicit}"`. The download-time fallbacks used when the config key is unset (`backend/services/download.py (_build_dl_config_kwargs)`): `"{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]"` and `"{tracknumber:02d}. {artist} - {title}"`. A fresh install downloads with the second layout while the Settings preview shows the first. Which default is canonical is undecided (open question — confirm with maintainer before "fixing" either side). |
| W10 | Legacy `/api/downloads/scan` hardcodes `/music` | `backend/api/downloads.py (scan_downloads)` resolves the downloads path as `db.get_config("downloads_path") or "/music"`, skipping `STREAMRIP_DOWNLOADS_PATH` — in local dev with no config key it scans `/music` and finds nothing. It also imports the Qobuz SDK's `scan_downloaded_albums` regardless of source, and bypasses the I1 primitives entirely. |
| W11 | No CORS middleware, no exception handlers | Grep for `CORSMiddleware`, `add_middleware`, `exception_handler` in `backend/` returns nothing. A frontend served from a different origin cannot call the API, and unhandled service exceptions surface as bare 500s with default bodies. |
| W12 | External CDN coupling | Album covers are hotlinked from the streaming services' CDNs (`<img src={album.cover_url}>` in `frontend/src/lib/components/AlbumCard.svelte`), and the Atkinson Hyperlegible font loads from `fonts.googleapis.com` (`frontend/src/app.html`). A fully offline deployment loses cover art and silently falls back to a sans-serif font. |
| W13 | Frontend node tests are unwired from CI | `frontend/tests/*.test.js` use `node:test`, but `frontend/package.json` has no `test` script, `make test` runs pytest only, and the CI `frontend-build` job only runs `npm ci && npm run build`. Run them manually with `node --test frontend/tests/`. Whether they should be wired into CI is undecided (inferred — confirm with maintainer). |

Additional standing constraint (not a weak point): everything internal still says "streamrip" — module names, `STREAMRIP_*` env vars, `streamrip.db`, the `.streamrip.json` sentinel, the `logging.getLogger("streamrip")` logger, the Docker volume. This is deliberate; the full rename is queued for v1.0 per CLAUDE.md. Do not rename any of it in an unrelated PR.

## 4. Checklist before merging an architectural change

1. If you touch download state outside `DownloadService`, confirm you went through the I1 primitives.
2. If you add a config key: is it credential-shaped? Then extend `cred_keys` in `backend/api/config.py` (I5) and document it per `libsync-config-and-flags`.
3. If you add an EventBus event the UI needs: add it to the bridge tuple in `backend/main.py (create_app)` AND a frontend `onEvent` handler (I6), and prove delivery end-to-end.
4. If you touch dedup logic: keep the `dirname(STREAMRIP_DB_PATH)` derivation (I4) and the per-source filenames (D4) intact in ALL duplicated sites.
5. Run `make test` (~154 tests, seconds, no credentials needed) and rely on the CI checks (`backend-tests`, `frontend-build`, `docker-publish`, `ruff`) — do not bypass them. Note that local `ruff` may disagree with CI due to version skew; do not blindly `--fix` (see `libsync-validation-and-qa`).

## When NOT to use this skill

- Diagnosing a live failure symptom (stuck download, auth error, empty library): use `libsync-debugging-playbook`.
- Looking up a specific config key, default, or env var: use `libsync-config-and-flags`.
- Setting up the dev environment, submodule init, or build traps: use `libsync-build-and-env`.
- Running, deploying, releasing, or deciding what data files are safe to delete: use `libsync-run-and-operate`.
- Understanding codecs, quality tiers, OAuth flows, or the app-id/secret signing model: use `qobuz-tidal-domain-reference`.
- Deciding how a change must be gated and reviewed: use `libsync-change-control`.
- Test inventory and what counts as evidence before pushing: use `libsync-validation-and-qa`.
- History of past investigations and settled battles: use `libsync-failure-archaeology`.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it. All commands run from the repo root and are read-only.

- WS bridge tuple (I6, W2): `grep -n -A 9 'for event_type in' backend/main.py`
- `token_expired` never published: `grep -rn 'token_expired' backend --include='*.py'` (expect only main.py)
- Unbridged publishes (W2): `grep -rn '"album_status_changed"\|"scan_progress"\|"scan_complete"' backend --include='*.py'`
- Primitive call sites (I1): `grep -rn 'mark_album_downloaded\|unmark_album_downloaded' backend --include='*.py'`
- Direct `download_status` writers (I1 caveats): `grep -n 'update_album_status' backend/services/download.py backend/api/downloads.py`
- SDK dedup/sentinel writers (I1 caveat c): `grep -n '_mark_downloaded\|METADATA_FILENAME' sdks/qobuz_api_client/clients/python/qobuz/downloader.py`
- Config table sole accessors (I2): `grep -rn 'FROM config\|INSERT INTO config' backend --include='*.py'` (expect only models/database.py)
- Env fallback chain (I3): `grep -rn 'STREAMRIP_DOWNLOADS_PATH\|STREAMRIP_DB_PATH' backend --include='*.py'`
- Dedup dir derivation and filenames (I4, D4): `grep -n 'downloads-\|downloads.db' backend/services/download.py backend/services/scan.py backend/api/library.py`
- Hot-reload cred keys (I5): `grep -n -A 7 'cred_keys' backend/api/config.py`
- No auth (W1): `grep -rn 'Depends\|HTTPBearer\|Security\|APIKey' backend --include='*.py'` (expect no matches)
- No CORS/exception handlers (W11): `grep -rn 'CORSMiddleware\|add_middleware\|exception_handler' backend --include='*.py'` (expect no matches)
- Tidal token persistence gap (W6): `grep -rn 'set_config("tidal_access_token"' backend --include='*.py'` (expect only api/auth.py)
- `status='all'` asymmetry (W7): `grep -n 'status != "all"' backend/models/database.py` vs `grep -n -A 4 'def count_albums' backend/models/database.py`
- Default format mismatch (W9): `grep -n 'folder_format\|track_format' backend/models/schemas.py backend/services/download.py`
- Schema version and backfill regex (D8): `grep -n 'SCHEMA_VERSION\|kHz' backend/models/database.py`
- Quality keys (D5): `grep -n '_quality' backend/services/download.py`
- Factory entrypoint (D1): `grep -rn 'create_app --factory' Makefile docker/Dockerfile`
- Editable SDK installs (D3): `grep -n 'install -e' Makefile`
- SPA fallback and realpath guard (D9): `grep -n -A 10 'serve_frontend' backend/main.py`
- Frontend SPA/runes config (D10): `cat frontend/svelte.config.js frontend/src/routes/+layout.ts`
- Google Fonts CDN (W12): `grep -n 'fonts.googleapis' frontend/src/app.html`
- Frontend tests unwired (W13): `grep -n '"test"' frontend/package.json` (expect no script) and `grep -n 'npm run' .github/workflows/pytest.yml`
- CI job names (checklist item 5): `grep -n '^  [a-z-]*:' .github/workflows/pytest.yml .github/workflows/ruff.yml`
