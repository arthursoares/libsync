---
name: libsync-config-and-flags
description: Use when a libsync setting or credential change does not take effect, STREAMRIP_DB_PATH or STREAMRIP_DOWNLOADS_PATH seems ignored, a Settings toggle silently inverts, downloads land in an unexpected folder layout, auto-sync never fires or fires late, GET or PATCH /api/config returns 500 or unexpected defaults, .streamrip.json sentinels stop being written, or when adding, renaming, or auditing any config key or environment variable.
---

# libsync Config and Flags Catalog

Libsync has exactly two environment variables and one key/value `config` table inside the main SQLite database (`data/streamrip.db` by default). There is no TOML, YAML, or dotenv file. This skill catalogs every configuration axis, its default, its read site, and its traps. All facts verified against the repo as of 2026-07-03 (v0.0.6).

Definitions used throughout:

- **Config DB / config table**: the `config(key, value, updated_at)` table in the app SQLite database, accessed only through `AppDatabase.get_config` / `set_config` / `get_all_config` in `backend/models/database.py`. All values are stored as strings.
- **PATCH /api/config**: the REST endpoint (`backend/api/config.py`, `update_config`) the Settings page uses to write config. It stores every value with Python `str(value)`, so booleans persist as the strings `"True"` / `"False"`.
- **Dedup DB**: per-source SQLite files (`downloads.db` for Qobuz, `downloads-tidal.db` for Tidal) holding already-downloaded track IDs so re-downloads are skipped.
- **Sentinel**: the `.streamrip.json` file written into each downloaded album folder.

## 1. Environment variables — there are exactly two

Verify the claim "exactly two" any time: `grep -rn "os.environ" backend --include='*.py'` returns only these.

| Env var | Default | Read at | Purpose |
|---|---|---|---|
| `STREAMRIP_DB_PATH` | `data/streamrip.db` | `backend/main.py` (`create_app`), `backend/api/library.py` (`_dedup_db_dir`), `backend/services/download.py` (`_download_album`) | Path to the app SQLite DB. The dedup DBs live in `dirname(STREAMRIP_DB_PATH)` — they follow the DB, not the music folder. |
| `STREAMRIP_DOWNLOADS_PATH` | `/music` | `backend/main.py` (`create_app`), `backend/api/library.py` (`_resolve_downloads_root`), `backend/services/download.py` (`_build_dl_config_kwargs`) | Downloads root fallback when the DB has no `downloads_path` key. |

**PRECEDENCE RULE — READ THIS TWICE.** The downloads root resolves as: DB config key `downloads_path` → `STREAMRIP_DOWNLOADS_PATH` env var → `/music`. The DB wins. **Setting `STREAMRIP_DOWNLOADS_PATH` on a deployment whose DB already contains a `downloads_path` key does NOTHING.** If a user reports "I changed the env var and downloads still go to the old place", run `sqlite3 data/streamrip.db "SELECT value FROM config WHERE key='downloads_path'"` — a non-empty result is the answer. Fix it via Settings or `PATCH /api/config {"downloads_path": "..."}`, not the env var.

Known inconsistency (as of 2026-07-03): the legacy endpoint `POST /api/downloads/scan` (`backend/api/downloads.py`, `scan_downloads`) resolves the downloads root as `db.get_config("downloads_path") or "/music"` — it skips the env var entirely. In local dev with no `downloads_path` config key it scans `/music` and finds nothing. Whether this is intentional is an open question (inferred oversight — confirm with maintainer).

Where the env vars are set in practice: `make dev` / `make dev-backend` export `STREAMRIP_DB_PATH=data/streamrip.db STREAMRIP_DOWNLOADS_PATH=~/Downloads/Music` (Makefile); Docker compose files set `STREAMRIP_DB_PATH=/data/streamrip.db` and mount `/music`.

## 2. Config table catalog

The keys below are the complete set read anywhere in `backend/` (re-verify: `grep -rn "get_config\|set_config" backend --include='*.py'`). Names in the "PATCHable" column refer to fields on `ConfigUpdate` in `backend/models/schemas.py` — a key absent from `ConfigUpdate` cannot be written through the API at all.

### 2a. Credentials

| Key | Default when unset | Where read | In Settings UI? | PATCHable? | Notes / traps |
|---|---|---|---|---|---|
| `qobuz_token` | none (Qobuz client not created) | `backend/main.py` (`_init_clients`, `_resolve_qobuz_credentials`) | Yes | Yes | PATCHing a **changed** token without also sending `qobuz_app_id` auto-pins `qobuz_app_id='798273057'` (see trap 5). |
| `qobuz_user_id` | none | `backend/api/auth.py` (`auth_status`) | Yes | Yes | Written by the OAuth flows alongside the token. |
| `qobuz_app_id` | `'798273057'` (fallback in `_init_clients`) | `backend/main.py` (`_init_clients`) | Yes (advanced field) | Yes | Must match the app that issued the token or every Qobuz call 401s. OAuth flow writes `'304027809'` (`backend/api/auth.py`). `_resolve_qobuz_credentials` caches the spoofed bundle app_id on first boot but never overwrites an existing value. |
| `qobuz_app_secret` | none (triggers resolution chain) | `backend/main.py` (`_resolve_qobuz_credentials`, step 1) | Yes (advanced field) | Yes | User override; when unset, resolution falls through to the hardcoded OAuth-app secret, then the spoofer scrape. Domain detail lives in the `qobuz-tidal-domain-reference` skill. |
| `tidal_access_token` | none (Tidal client not created) | `backend/main.py` (`_init_clients`) | Shown as connected/not-connected only | **No** — not a `ConfigUpdate` field | Written only by the Tidal device-code and PKCE auth endpoints (`backend/api/auth.py`), which call `_reload_clients` themselves. |
| `tidal_refresh_token` | none | `backend/main.py` (`_init_clients`) | No | No | The SDK refreshes tokens in memory but **nothing persists refreshed access tokens back to the DB** (grep-verified: `set_config("tidal_access_token"...)` appears only in `backend/api/auth.py`). Works only as long as the stored refresh token stays valid. |
| `tidal_user_id` | `0` | `backend/main.py` (`_init_clients`) | No | No | |
| `tidal_country_code` | `'US'` | `backend/main.py` (`_init_clients`) | No | No | |
| `tidal_token_expiry` | `'0'` | `backend/main.py` (`_init_clients`) | No | No | Parsed with `float()`; unparseable values fall back to `0.0`. Goes stale immediately (see refresh note above). |
| `tidal_auth_method` | `'device_code'` | `backend/main.py` (`_init_clients`) | Read-only (Settings shows a PKCE badge) | No | Set to `'pkce'` by the PKCE completion endpoint. |

Security note: `GET /api/config` returns `qobuz_token`, `qobuz_app_secret`, and `tidal_access_token` in plaintext and no endpoint requires authentication. Deployment safety relies on network placement. Do not paste `GET /api/config` output into public issues.

### 2b. Download settings

All of these are read fresh from the config DB on every download (`backend/services/download.py`, `_build_dl_config_kwargs` and `_download_album`), so changes apply to the **next** download without a restart. All are user-facing in `frontend/src/routes/settings/+page.svelte` and PATCHable.

| Key | Default when unset | Where read | Notes / traps |
|---|---|---|---|
| `downloads_path` | chain: DB → `STREAMRIP_DOWNLOADS_PATH` → `/music` | `download.py` (`_build_dl_config_kwargs`), `library.py` (`_resolve_downloads_root`), `main.py` (`create_app`) | See Section 1 precedence rule. Also gates `mark-downloaded` path validation (`_validate_local_folder_path` rejects paths outside this root with 400). |
| `qobuz_quality` | `3` | `download.py` (`_download_album`, key `f"{source}_quality"`) | `int()` crash path (trap 4). Settings UI offers values 1–4; the Qobuz SDK docstring defines quality as 1–4 (`sdks/.../qobuz/downloader.py`). Note: CLAUDE.md summarizes both as a "0–3 scale" — treat the SDK code as ground truth for exact values; tier meaning belongs to `qobuz-tidal-domain-reference`. |
| `tidal_quality` | `3` | same as above | Settings UI offers 0–4 (4 labeled "coming soon"); the Tidal SDK defines 0=LOW, 1=HIGH, 2=LOSSLESS, 3=HI_RES and clamps input to 0–4. |
| `max_connections` | `6` (`DownloadService.__init__` default) | `download.py` (`_build_dl_config_kwargs`) | `int()` crash path (trap 4). |
| `folder_format` | `{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]` (download-time fallback) | `download.py` (`_build_dl_config_kwargs`) | **DEFAULT MISMATCH** — see trap 3. |
| `track_format` | `{tracknumber:02d}. {artist} - {title}` (download-time fallback) | `download.py` (`_build_dl_config_kwargs`) | **DEFAULT MISMATCH** — see trap 3. |
| `embed_artwork` | `True` | `download.py` via `_parse_bool` | Passed to the SDK as `embed_cover`. |
| `artwork_size` | `'large'` | `download.py` (`_build_dl_config_kwargs`, as `cover_size`) | UI options: `thumbnail`, `small`, `large`, `original`. |
| `source_subdirectories` | `False` | `download.py` via `_parse_bool` | |
| `disc_subdirectories` | `True` | `download.py` via `_parse_bool` | |
| `qobuz_download_booklets` | `True` | `download.py` via `_parse_bool` | Only passed for `source == "qobuz"`. |

### 2c. Sync and scan settings

| Key | Default when unset | Where read | In Settings UI? | Notes / traps |
|---|---|---|---|---|
| `auto_sync_enabled` | `'false'` | `backend/main.py` (`_start_auto_sync_if_enabled`) | Yes | Parsed case-insensitively: `.lower() in ("true", "1", "yes")`. PATCHing restarts the loop (trap 7). Auto-sync is single-source: first connected of qobuz, tidal. |
| `auto_sync_interval` | 6 hours | `backend/main.py` (`_parse_auto_sync_interval`) | Yes | Accepts presets `1h`, `6h`, `12h`, `daily`, `24h`, or a bare integer string = seconds with a floor of 60. **Anything unparseable — including the literal string `custom` that the Settings "Custom cron" option stores — silently falls back to 6 hours** (trap 9). |
| `scan_sentinel_write_enabled` | `'True'` | `backend/api/library.py` (`mark_downloaded`, `start_scan`) | Yes | Read **case-sensitively** as `(... or "True") == "True"` — trap 2. Controls whether `.streamrip.json` sentinels are written by the mark-downloaded and fuzzy-scan paths. |

## 3. Traps

1. **Env var silently ignored.** `downloads_path` in the DB overrides `STREAMRIP_DOWNLOADS_PATH`. See Section 1. This is the single most common config confusion.
2. **Booleans are stored as `'True'`/`'False'`, and two read sites are case-sensitive.** `PATCH /api/config` persists `str(value)`, so Pydantic booleans land as `"True"`/`"False"`. `backend/services/download.py` defines `_parse_bool` (case-insensitive, accepts `true`/`1`/`yes`) precisely because case-sensitive comparisons silently inverted user toggles (regression test: `tests/test_download_config_bools.py`). But `backend/api/library.py` still reads `scan_sentinel_write_enabled` with a case-SENSITIVE `== "True"` in both `mark_downloaded` and `start_scan` — a hand-written lowercase `'true'` in the config table (e.g. via direct `sqlite3` edit) silently disables sentinel writes. When touching any boolean config read, always use `_parse_bool`-style parsing.
3. **`folder_format`/`track_format` schema-vs-download default mismatch.** The `AppConfig` schema in `backend/models/schemas.py` declares defaults `{albumartist}/({year}) {title} [{container}-{bit_depth}-{sampling_rate}]` and `{tracknumber:02}. {artist} - {title}{explicit}`; the download-time fallbacks in `backend/services/download.py` (`_build_dl_config_kwargs`) are `{albumartist} - {title} ({year}) [{container}] [{bit_depth}B-{sampling_rate}kHz]` and `{tracknumber:02d}. {artist} - {title}`. On a fresh install with the keys unset, the Settings preview shows the schema default while actual downloads use the download.py fallback — different directory layouts. Which default is canonical is an open question (confirm with maintainer). Once a user saves Settings, the stored value wins everywhere and the mismatch disappears.
4. **`int()` crash paths on non-numeric values.** `backend/services/download.py` runs `int(q_val)` on `{source}_quality` and `int(...)` on `max_connections` — a non-numeric stored value raises `ValueError` mid-download, surfacing as a generic failed download rather than a config error. `GET /api/config` (`backend/api/config.py`) also runs `int(value)` on `qobuz_quality`, `tidal_quality`, and `max_connections`, so a corrupt value 500s the whole config endpoint. The API types these as `int` so PATCH cannot store garbage; only direct DB writes can — another reason to never hand-edit the config table.
5. **Changed `qobuz_token` auto-pins the app id.** `PATCH /api/config` with a `qobuz_token` that differs from the stored one, and no explicit `qobuz_app_id`, sets `qobuz_app_id='798273057'` (web-player app; `backend/api/config.py`, `update_config`). If you are PATCHing an OAuth-issued token (app `304027809`) programmatically, you MUST include the matching `qobuz_app_id` in the same PATCH or every subsequent Qobuz call 401s. The UI OAuth flow avoids this by using `/api/auth/qobuz/*` endpoints, which set both atomically.
6. **Credential PATCH hot-reloads SDK clients and can kill in-flight downloads.** When a PATCH touches any of `qobuz_token`, `qobuz_user_id`, `qobuz_app_id`, `qobuz_app_secret`, `tidal_access_token`, `_reload_clients` (`backend/api/config.py`) calls `__aexit__` on the old clients before building new ones — a download using the old session mid-transfer fails. Do not change credentials while downloads are running. (Note: `tidal_access_token` in that trigger set is unreachable via PATCH because `ConfigUpdate` has no such field; Tidal reloads happen through the auth endpoints instead.)
7. **PATCHing `auto_sync_enabled`/`auto_sync_interval` restarts the loop, and the loop sleeps a FULL interval before its first run.** `SyncService.start_auto_sync` (`backend/services/sync.py`) runs `await asyncio.sleep(interval_seconds)` before the first sync. Enabling auto-sync does not trigger an immediate sync, and every settings save that touches these keys resets the countdown. Users expecting instant effect should press "Sync now".
8. **Tidal credentials cannot be set through `PATCH /api/config`.** `ConfigUpdate` has no `tidal_access_token`/`tidal_refresh_token` fields; the only writers are the device-code and PKCE endpoints in `backend/api/auth.py`. Scripted Tidal credential injection requires either those endpoints or a direct DB write plus restart (direct write is discouraged — see traps 2 and 4).
9. **The Settings "Custom cron" interval option stores the literal string `custom`,** which `_parse_auto_sync_interval` cannot parse, so it silently becomes the 6-hour default. To get a real custom interval, PATCH `auto_sync_interval` with a bare seconds string (minimum 60), e.g. `{"auto_sync_interval": "900"}`. (UI gap, inferred oversight — confirm with maintainer.)
10. **`POST /api/config/reset` clears library data but preserves the config table** (credentials survive) and touches neither the dedup DBs nor sentinels — re-downloads after a reset are still skipped unless forced. Conversely, deleting `streamrip.db` destroys credentials, not just library state, because they live in the same file.

## 4. How to add a config option

Checklist derived from the existing patterns in this repo (the pattern itself is inferred from code, not documented by the maintainer — confirm house rules before institutionalizing deviations):

1. **Schema**: add the field to BOTH `AppConfig` (with its default) and `ConfigUpdate` (as `<type> | None = None`) in `backend/models/schemas.py`. Keep the `AppConfig` default identical to the read-site default (avoid recreating trap 3).
2. **GET coercion**: if the value is an `int`, add its key to the int list in `get_config` (`backend/api/config.py`); if a `bool`, add it to the bool list there. Missing this makes GET return a raw string and the Settings page misbehave.
3. **Read site**: read with `db.get_config("<key>")` and an explicit inline default. Parse booleans with `_parse_bool` from `backend/services/download.py` (case-insensitive) — never `== "True"`.
4. **Settings UI**: in `frontend/src/routes/settings/+page.svelte`, add a `$state` variable, populate it in the config-load block (the `config.<key> ?? <default>` assignments), render a control, and include the key in the PATCH body built for save.
5. **Hot-reload decision**: does changing the value require action beyond the next read? Download settings need nothing (read per-download). Credentials must join the `cred_keys` set and auto-sync keys the `auto_sync_keys` set in `update_config` (`backend/api/config.py`). Document which behavior you chose.
6. **Tests**: add a PATCH-then-GET round-trip like `tests/test_api_routes.py` (`test_update_config`); booleans additionally get a casing regression test like `tests/test_download_config_bools.py`. Run `make test`.
7. **Change control**: CHANGELOG entry; land via PR with the required CI checks (backend-tests, frontend-build, docker-publish, ruff). See the `libsync-change-control` skill.
8. **This skill**: add a row to the relevant table in Section 2 and a re-verification line to Section 6.

## 5. When NOT to use this skill

- Recreating the dev environment, submodule init, or build failures → `libsync-build-and-env`.
- Running, deploying, releasing, or deciding which data files are safe to delete → `libsync-run-and-operate` (this skill covers what the keys *mean*; that one covers operating the artifacts).
- Qobuz/Tidal quality-tier semantics, codecs, OAuth theory, app-id/secret model → `qobuz-tidal-domain-reference`.
- Actively debugging a live failure (401s, downloads failing) → `libsync-debugging-playbook`; Qobuz auth resilience work → `libsync-qobuz-auth-campaign`.
- Design invariants behind the config system (single DB, no config file) → `libsync-architecture-contract`.

## 6. Provenance and maintenance

Every fact above was verified against the working tree on 2026-07-03 (v0.0.6). Re-verify before trusting:

- Exactly two env vars: `grep -rn "os.environ" backend --include='*.py'`
- Full config key inventory: `grep -rn "get_config\|set_config" backend --include='*.py'`
- Env-precedence chain and format-string download fallbacks: `grep -n "downloads_path\|folder_format\|track_format" backend/services/download.py`
- Schema defaults (`AppConfig`/`ConfigUpdate` fields): `grep -n "class AppConfig" -A 45 backend/models/schemas.py`
- GET int/bool coercion lists and PATCH auto-pin / hot-reload / auto-sync trigger sets: `sed -n '13,82p' backend/api/config.py`
- Case-sensitive sentinel reads: `grep -n 'scan_sentinel_write_enabled' backend/api/library.py`
- Case-insensitive `_parse_bool`: `grep -n "_parse_bool" -A 9 backend/services/download.py`
- Auto-sync interval presets, 60s floor, 6h fallback: `grep -n "_parse_auto_sync_interval" -A 25 backend/main.py`
- Auto-sync sleeps before first run: `grep -n "asyncio.sleep(interval_seconds)" -B 3 backend/services/sync.py`
- Settings UI "custom" option stores literal string: `grep -n '"custom"' frontend/src/routes/settings/+page.svelte`
- Tidal token never re-persisted after SDK refresh: `grep -rn 'set_config("tidal_access_token"' backend --include='*.py'` (hits only `backend/api/auth.py`)
- `/api/downloads/scan` ignores the env var: `grep -n 'downloads_path' backend/api/downloads.py`
- Qobuz/Tidal SDK quality scales: `grep -n "quality" sdks/qobuz_api_client/clients/python/qobuz/downloader.py sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py | head`
- Dev/Docker env var values: `grep -rn "STREAMRIP" Makefile docker/ docker-compose.example.yml`

Grep hygiene reminder: exclude `.claude/`, `node_modules/`, `.svelte-kit/`, and `sdks/` from repo-root greps unless you intend to search them — `.claude/worktrees/` contains full copies of deleted CLI-era code that will pollute results.
