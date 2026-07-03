---
name: libsync-diagnostics-and-tooling
description: Use when a libsync symptom needs measurement instead of eyeballing - this skill ships runnable read-only scripts to dump the config table with secrets redacted, probe what actually arrives on the live WebSocket, report album download_status states, reconcile .streamrip.json sentinels against the library DB, inspect the per-source dedup DBs behind silently skipped re-downloads, or health-check a fresh checkout; also use when you need visible backend INFO logs via the shipped uvicorn --log-config (the backend configures no logging of its own).
---

# libsync Diagnostics and Tooling

This skill replaces eyeballing with measurement. It ships seven runnable, read-only tools in `scripts/` and documents the measurement surfaces the backend already exposes. Every script was executed against a real checkout and real data before shipping (as of 2026-07-03, v0.0.6).

Jargon used throughout, defined once:

- **App DB** — the main SQLite file (default `data/streamrip.db`, overridden by the `STREAMRIP_DB_PATH` env var). Holds the `albums`, `tracks`, `sync_runs`, `config`, and `schema_version` tables. Credentials live in the `config` table of this same file.
- **Dedup DB** — a per-source SQLite file with one table, `downloads(id TEXT PRIMARY KEY)`, listing track IDs already downloaded. Qobuz uses `downloads.db`; Tidal uses `downloads-tidal.db`. Both live in the directory of `STREAMRIP_DB_PATH`, NOT under the downloads path (see `_dedup_db_dir` in `backend/api/library.py` and the equivalent derivation in `backend/services/download.py` `_download_album`).
- **Sentinel** — a `.streamrip.json` file written into each downloaded album's folder. Payload keys: `source`, `album_id`, `title`, `artist`, `tracks_count`, `downloaded_at` (see `_sentinel_payload` in `backend/services/scan.py`).

## Ground rules

1. Run everything from anywhere inside the repo — scripts locate the repo root via `git rev-parse`.
2. All scripts are read-only against the app DB and dedup DBs (SQLite is opened in `mode=ro`). None of them prints a full token or secret value.
3. Scripts accept paths as arguments and fall back to `STREAMRIP_DB_PATH` / `STREAMRIP_DOWNLOADS_PATH`, then to the repo defaults (`data/streamrip.db`, `/music`).
4. Nothing here talks to live Qobuz or Tidal APIs. Do not "fix" a finding by starting the real backend with real credentials unless you understand that startup may hit `play.qobuz.com` (Qobuz secret spoofer in `backend/main.py` `_resolve_qobuz_credentials`).

## Script inventory

| Script | Question it answers |
|---|---|
| `scripts/dump-config.sh` | What is actually stored in the config table, verbatim, with secrets redacted? |
| `scripts/check-dedup.sh` | Why do re-downloads skip tracks? |
| `scripts/album-status-report.sh` | What state are my albums in; are any stuck from a restart? |
| `scripts/check-sentinels.sh` | Do the on-disk sentinels and the DB agree? |
| `scripts/ws-probe.py` | What is actually arriving on the WebSocket? |
| `scripts/logging-config.json` | How do I see the backend's INFO logs at all? |
| `scripts/env-doctor.sh` | Is this checkout capable of running the app and the tests? |

## Step 0: make runtime logging visible (do this first)

The single most important fact for diagnosing libsync: **the backend configures no logging**. All 11 backend modules log to `logging.getLogger("streamrip")`, but no `basicConfig`/`dictConfig` exists anywhere in `backend/`, and neither the Makefile dev targets nor the Docker `CMD` pass `--log-config`. Under uvicorn's default logging, the `streamrip` logger has no handler: INFO and DEBUG are silently dropped; only WARNING+ leaks out via Python's last-resort handler, without timestamps.

Verified empirically (2026-07-03): starting uvicorn without a log config and shutting it down prints nothing for the shutdown INFO line `streamrip web UI shutting down`; starting it with the shipped config prints `2026-07-03 10:03:55,564 INFO streamrip streamrip web UI shutting down`.

**Trap:** pytest DOES show these logs (`log_cli = true`, `log_level = "DEBUG"` in `pyproject.toml`). Seeing logs in `make test` does not mean the running server shows them. Do not add `logger.info(...)` diagnostics and conclude "the code path never runs" because nothing printed.

To run the backend with visible INFO logs, use the shipped dictConfig:

```bash
STREAMRIP_DB_PATH=data/streamrip.db STREAMRIP_DOWNLOADS_PATH=~/Downloads/Music \
  poetry run uvicorn backend.main:create_app --factory --port 8080 --reload \
  --log-config .claude/skills/libsync-diagnostics-and-tooling/scripts/logging-config.json
```

This is the `make dev-backend` command plus `--log-config`. The config sets the `streamrip` logger to INFO with timestamps and keeps uvicorn's own loggers at INFO. Raise `streamrip` to `DEBUG` in the JSON if you need more.

## scripts/dump-config.sh — config table, verbatim

```bash
.claude/skills/libsync-diagnostics-and-tooling/scripts/dump-config.sh [db-path]
```

Prints every `config` row as `key`, `repr(value)`, `updated_at`. Keys containing `token` or `secret` show only length and last 4 characters (`REDACTED(len=451, ...Nyjg)`); `tidal_token_expiry` is a timestamp and shown in full, with an automatic staleness note.

Interpretation:

| Observation | Meaning |
|---|---|
| `scan_sentinel_write_enabled` is `'true'` (lowercase) | Sentinel writes are SILENTLY DISABLED. `backend/api/library.py` (mark_downloaded and start_scan handlers) compares case-sensitively: `(db.get_config(...) or "True") == "True"`. Only exact `'True'` (or absent key) enables writes. Download-setting booleans, by contrast, go through case-insensitive `_parse_bool` in `backend/services/download.py`. |
| `tidal_token_expiry` far in the past | Expected. Only the Tidal auth endpoints (`backend/api/auth.py`, device-code poll and pkce-complete) ever persist tokens; the SDK's in-process refreshes are never written back. Only a problem if the stored `tidal_refresh_token` stops working. |
| `qobuz_app_id` = `'304027809'` | OAuth-issued token; the app secret comes from the hardcoded `qobuz.auth.APP_SECRET`. |
| `qobuz_app_id` = `'798273057'` | Manually pasted web-player token; the secret is scraped from `play.qobuz.com` at startup. A mismatch between app_id and the app that issued the token means 401 on every Qobuz endpoint. |
| `downloads_path` is `''` (empty string) | Falsy — the backend falls through to `STREAMRIP_DOWNLOADS_PATH`, then `/music`. An empty value is not the same as a set value. |
| Suspicious trailing whitespace or casing anywhere | `repr()` output makes it visible; the config store does no normalization. |

## scripts/check-dedup.sh — why re-downloads skip

```bash
.claude/skills/libsync-diagnostics-and-tooling/scripts/check-dedup.sh [dedup-db-dir]
```

Reports existence, row count, and 5 sample track IDs for `downloads.db` (Qobuz) and `downloads-tidal.db` (Tidal).

Interpretation:

- Every track ID present is **skipped** on re-download: `DownloadService._download_album` passes the dedup DB path to the SDK as `downloads_db_path` unless the item has `force=True`.
- "I deleted the files but re-download does nothing" — the track IDs are still in the dedup DB. Fix properly via the album's "unmark as downloaded" action (`unmark_album_downloaded` removes the IDs), or re-download with force.
- `POST /api/config/reset` deletes albums/tracks/sync_runs but does NOT clear dedup DBs — after a reset, previously downloaded albums still skip.
- A missing `downloads-tidal.db` is normal — it is created lazily on first Tidal download or mark.
- Dedup DBs are safe to delete outright; the only loss is skip-already-downloaded state.
- Rows appear here from two writers: the SDK during real downloads, and `mark_album_downloaded` in `backend/services/scan.py` (scan auto-confirm and the manual "Mark as downloaded" button).

## scripts/album-status-report.sh — album states and restart artifacts

```bash
.claude/skills/libsync-diagnostics-and-tooling/scripts/album-status-report.sh [db-path]
```

Prints counts per `source` x `download_status`, then flags stuck rows.

Interpretation:

- **`queued` / `downloading` rows while nothing is downloading are restart artifacts.** The download queue is in-memory only (`DownloadService._queue`); a backend restart forgets the queue but nothing resets `albums.download_status` on boot (verified: no reset in `backend/main.py` `create_app`, as of 2026-07-03). Re-enqueue those albums.
- Expect virtually no `failed` rows from the download pipeline: its failure path sets the album back to `not_downloaded` (see the exception handler in `DownloadService`, `backend/services/download.py`), even though the queue history query filters `IN ('complete','failed')`.
- The script also counts `complete` albums with NULL `local_folder_path` — these were marked before schema v2 or reconciled via the legacy `POST /api/downloads/scan`, which records no folder. `check-sentinels.sh` cannot tie them to disk.

## scripts/check-sentinels.sh — disk vs DB reconciliation

```bash
.claude/skills/libsync-diagnostics-and-tooling/scripts/check-sentinels.sh <music-root> [db-path]
```

Walks the music root (skipping symlinked directories, same as the backend scanner), validates every sentinel's JSON payload against the expected six keys, and cross-checks both directions against the app DB.

Interpretation:

- **Sentinel-but-not-marked**: the folder claims downloaded, the DB disagrees. Run the fuzzy scan (`POST /api/library/scan-fuzzy`) to reconcile — it short-circuits on sentinel presence and marks the album.
- **Marked-but-no-sentinel**: DB says complete but no sentinel exists under this root. Causes: folder moved/deleted, sentinel writes disabled (see the case-sensitivity trap above), or the album marked with no folder recorded.
- **Invalid sentinels** (bad JSON / missing keys): hand-edited or truncated writes; the scanner will not honor them.
- Trap when reconciling: the legacy `POST /api/downloads/scan` resolves its root as `db.get_config("downloads_path") or "/music"` — it IGNORES `STREAMRIP_DOWNLOADS_PATH` (verified in `backend/api/downloads.py`, scan_downloads handler, as of 2026-07-03). In local dev with no `downloads_path` config key it scans `/music` and finds nothing. Prefer `POST /api/library/scan-fuzzy`, which uses the full resolution chain.

## scripts/ws-probe.py — what actually arrives on the WebSocket

```bash
poetry run python .claude/skills/libsync-diagnostics-and-tooling/scripts/ws-probe.py [ws://HOST:8080/api/ws] [--idle-timeout N]
```

Connects to the WebSocket endpoint and prints one line per event: timestamp, type, compact payload summary. Uses the `websockets` package, a direct dependency of this project. The endpoint discards all inbound messages, so the probe cannot mutate anything.

Interpretation — the ONLY event types you can ever see (the bridge list in `backend/main.py` `create_app`, as of 2026-07-03, v0.0.6):

| Type | When |
|---|---|
| `download_progress` | During a download; throttled to at most one emit per 0.5s |
| `download_complete` | Album finished |
| `download_failed` | Album failed |
| `sync_started` / `sync_complete` | Library sync lifecycle |
| `library_updated` | Library refresh finished |
| `token_expired` | **Never** — bridged to the WebSocket but no backend code publishes it (dead wire, grep-verified) |

Events you will NEVER see, no matter what the docs or frontend suggest: `scan_progress`, `scan_complete`, and `album_status_changed`. They are published to the in-process EventBus (`backend/services/scan.py`, `backend/api/library.py`) but are not in the bridge list, and the EventBus silently drops unsubscribed events. The frontend listener for `album_status_changed` (`frontend/src/lib/stores/library.ts`) can never fire. Scan progress reaches the UI exclusively by polling `GET /api/library/scan-fuzzy/{job_id}` — that is intentional per the comment in `backend/api/library.py`. If the probe flags a type outside the table, the bridge list changed: update `BRIDGED_TYPES` in the script and this table.

## scripts/env-doctor.sh — dev environment health

```bash
.claude/skills/libsync-diagnostics-and-tooling/scripts/env-doctor.sh
```

PASS/FAIL/WARN lines for: submodule initialized at the pinned commit, submodule working-tree dirtiness, `qobuz`/`tidal`/`websockets` importable in the poetry venv, ffmpeg on PATH, venv Python vs the CI pin (3.12), node vs the CI pin (20), `backend/static/index.html` present, app DB present. Exits non-zero only on hard failures (uninitialized submodule, un-importable SDKs). Fix import failures with `make deps`; version drift from CI pins is a WARN because `pyproject.toml` allows Python `>=3.10 <4.0`. A dirty submodule tree is a WARN: `make deps` or `git submodule update` may clobber local edits there.

## Measurement surfaces the backend already exposes

- `GET /api/health` → `{"status": "ok"}`. Liveness only.
- `GET /api/auth/status` → per-source `{authenticated, user_id, has_credentials}`. **Known weakness** (as of 2026-07-03): `authenticated` only checks that `client._transport._session is not None` (`backend/api/auth.py`, auth_status handler) — it proves an HTTP session object exists, not that the token works. A Qobuz token can be expired (401 on every call) while this reports `authenticated: true`. `has_credentials` only checks the config key exists. Treat this endpoint as "client constructed", nothing more.
- `GET /api/downloads/queue` → in-memory queue items merged with the last 50 complete/failed albums from the DB. `active_count` and `total_speed` cover only items currently `downloading`.
- `POST /api/library/scan-fuzzy` → `{"job_id": ...}` (409 if a scan is already running); poll `GET /api/library/scan-fuzzy/{job_id}` → `{"status": "running", "scanned": N, "total": M}` then `{"status": "complete", ...}`. This polling endpoint is the only scan-progress surface (see ws-probe section).
- SQLite one-liners (read-only):

```bash
# Schema version — expect 2 (as of 2026-07-03, v0.0.6)
sqlite3 data/streamrip.db "SELECT version FROM schema_version;"

# Index inventory — expect idx_albums_source_user, idx_albums_download_status, idx_tracks_album
sqlite3 data/streamrip.db "PRAGMA index_list(albums); PRAGMA index_list(tracks);"

# Sync run history for one source
sqlite3 data/streamrip.db "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 5;"
```

Note the app DB runs in WAL mode (write-ahead logging; `-wal`/`-shm` sidecar files are normal). The dedup DBs are opened without WAL or a timeout by the scan primitives, while the SDK writes the same file during downloads — a scan during an active download can hit `database is locked` (inferred from code reading, not reproduced — confirm with maintainer before treating as a bug).

## When NOT to use this skill

- You have a symptom and want the ranked likely causes → **libsync-debugging-playbook** (this skill supplies its instruments; that one supplies the triage).
- You need the meaning, default, and precedence of a specific config key or env var → **libsync-config-and-flags**.
- You are setting up or repairing the dev environment beyond a health check (installs, submodule surgery, Docker builds) → **libsync-build-and-env**.
- You are deciding what data files are safe to delete, or running/releasing the app → **libsync-run-and-operate**.
- You need OAuth/app-id/signing theory behind the Qobuz findings → **qobuz-tidal-domain-reference**; for the live Qobuz auth-resilience effort → **libsync-qobuz-auth-campaign**.
- You need to know what counts as merge evidence or the pre-push checklist → **libsync-validation-and-qa**. Never route around change control: fixes discovered with these tools still go through PRs and the required CI checks (backend-tests, frontend-build, docker-publish, ruff).

## Provenance and maintenance

Every fact above was verified against the repo on 2026-07-03 (v0.0.6). Re-verify before trusting:

- Bridged WebSocket event types (the 7-type list): `grep -n -A 10 'for event_type in' backend/main.py`
- `token_expired` still unpublished: `grep -rn token_expired backend/ frontend/src/`
- `scan_progress`/`scan_complete`/`album_status_changed` still unbridged: `grep -rn "publish(" backend/services/scan.py backend/api/library.py`
- Sentinel payload keys: `grep -n -A 10 '_sentinel_payload' backend/services/scan.py`
- Dedup DB names and directory derivation: `grep -n 'downloads.db\|downloads-' backend/services/scan.py backend/services/download.py backend/api/library.py`
- Case-sensitive sentinel-toggle read: `grep -n 'scan_sentinel_write_enabled' backend/api/library.py`
- Auth status session check: `grep -n -B 2 -A 6 '_transport._session' backend/api/auth.py`
- Legacy scan's hardcoded `/music` fallback: `grep -n 'downloads_path' backend/api/downloads.py`
- No logging config in the backend: `grep -rn 'basicConfig\|dictConfig\|addHandler' backend/ --include='*.py'` (expect no hits)
- pytest shows logs anyway: `grep -n 'log_cli\|log_level' pyproject.toml`
- CI pins (Python 3.12, Node 20, Poetry 1.8.0): `grep -n 'python-version\|node-version\|version: 1.8' .github/workflows/pytest.yml`
- Schema version constant: `grep -n 'SCHEMA_VERSION' backend/models/database.py`
- Tidal tokens only persisted by auth endpoints: `grep -rn 'set_config("tidal_access_token"' backend/`
- Progress throttle interval: `grep -n 'Throttle' backend/services/download.py`
- `websockets` still a direct dependency: `grep -n websockets pyproject.toml`
