---
name: libsync-debugging-playbook
description: "Use when libsync misbehaves at runtime: downloads 401 while the library loads, a settings toggle acts inverted or ignored, logger.info never prints, ModuleNotFoundError: No module named qobuz/tidal, make lint fails on untouched main, UI 404s while /api answers, a downloaded FLAC is not real FLAC, Tidal capped at AAC 320, scan stuck at 0/? or 409 'Another scan is already running', 'database is locked', albums stuck queued after restart, PKCE 400 'Unknown handle', or grep finds deleted code."
---

# Libsync Debugging Playbook

This is a symptom-to-cause triage runbook for libsync's known failure modes. Each entry gives the mechanism (verified in code), a discriminating check that separates it from look-alike causes, and the fix or the sibling skill that owns the fix. All paths are repo-relative. Line numbers cited below were checked on 2026-07-03 at v0.0.6 and will drift — prefer the named-function anchors.

Jargon used throughout, defined once:

- **app_id / X-App-Id**: the numeric Qobuz application identity sent as the `X-App-Id` HTTP header. It must match the app that issued the user's token. Two identities exist: `304027809` (OAuth/desktop-Helper app) and `798273057` (web-player app).
- **app secret**: the signing key paired with an app_id, used to MD5-sign `track/getFileUrl` requests (see `_compute_signature` in the SDK's `qobuz/streaming.py`). Browsing does not need it; downloading does.
- **dedup DB**: a tiny per-source SQLite file (`data/downloads.db` for Qobuz, `data/downloads-tidal.db` for Tidal) holding already-downloaded track IDs so re-downloads are skipped.
- **sentinel**: the `.streamrip.json` file written into each downloaded album folder, used by scans to recognize already-downloaded albums.
- **SPA catch-all**: the `GET /{path:path}` route in `backend/main.py` (`serve_frontend`) that serves the built frontend from `backend/static/`.
- **PKCE**: Proof Key for Code Exchange, the redirect-based Tidal OAuth variant that unlocks lossless/HiRes; the older device-code flow does not.
- **DASH**: Dynamic Adaptive Streaming over HTTP — Tidal delivers HiRes audio as fragmented MP4 segments that must be remuxed into a native FLAC container.
- **editable install**: `pip install -e <path>`, which links a package into the virtualenv from a source directory instead of copying it.

## First five minutes

Run this checklist in order before deep-diving. Each line routes you to a numbered section below or to a sibling skill.

1. **Is it the environment?** Run `poetry run python -c "import qobuz, tidal; print('sdk ok')"`. If it fails, go to section 4. For a from-scratch environment rebuild, use `libsync-build-and-env` instead.
2. **Is the app serving at all?** Run `curl -s http://localhost:8080/api/auth/status`. If the API answers but the browser shows 404s, go to section 6. If nothing answers, use `libsync-run-and-operate`.
3. **Is it auth?** Check `curl -s http://localhost:8080/api/auth/status` per source, then go to section 1 (Qobuz 401) or section 8 (Tidal quality). Note: auth status only proves a client object exists, not that the token works.
4. **Is it config?** Booleans and quality tiers live in the SQLite `config` table, not env vars. Run `sqlite3 data/streamrip.db "SELECT key, value FROM config"` (read-only) and go to sections 2 and 8. The full key catalog is in `libsync-config-and-flags`.
5. **Is it data state?** Album statuses, dedup DBs, and scan jobs: sections 9, 10, 11, 13.
6. **Do not trust two instruments**: absent INFO logs prove nothing (section 3), and repo-root grep results can show deleted code (section 15).
7. **Baseline sanity**: `make test` should print `154 passed` (as of 2026-07-03, v0.0.6, ~3s pytest time). If unit tests fail on clean main, your venv is broken — section 4.

## Master symptom table

| # | Symptom | Likely cause | Discriminating check | Fix / reference |
|---|---------|--------------|----------------------|-----------------|
| 1 | Library loads but downloads fail with 401 | Qobuz token expired, X-App-Id/token mismatch, or bad signing secret | Three-step experiment in section 1 | Section 1; deep recipe in `libsync-proof-and-analysis-toolkit` |
| 2 | Settings toggle behaves inverted or ignored | Pydantic stores booleans as `"True"`/`"False"` strings; a case-sensitive comparison site missed it | `sqlite3 data/streamrip.db "SELECT key,value FROM config WHERE value IN ('true','false','True','False')"` | Section 2 |
| 3 | `logger.info(...)` never prints at runtime | No logging config exists anywhere in `backend/` | Change the call to `logger.warning` — if that prints (bare, no timestamp), logging is unconfigured, not the code path | Section 3 |
| 4 | `ModuleNotFoundError: No module named 'qobuz'` (or `'tidal'`) | SDKs are editable pip installs invisible to Poetry; venv rebuild removed them | `poetry run pip list \| grep -i -e qobuz -e tidal` | `make deps` (section 4) |
| 5 | `make lint` fails on untouched main | Local ruff 0.1.15 vs CI ruff version skew; RUF100 false positives | `poetry run ruff --version` prints 0.1.15 and the 4 errors are all RUF100 | Section 5 — do NOT `--fix` |
| 6 | UI 404s but `/api/*` works | `make build-local` deleted `backend/static/` and the frontend build failed midway | `ls backend/static/index.html` | Re-run `make build-local`, watch npm output (section 6) |
| 7 | Downloaded `.flac` is not real FLAC; strict scanners choke | Tidal DASH delivers FLAC-in-MP4; ffmpeg missing so remux was skipped | `which ffmpeg`; `file path/to/track.flac` says "ISO Media" instead of "FLAC audio" | Install ffmpeg, force re-download (section 7) |
| 8 | Tidal downloads capped at AAC ~320 kbps | Device-code OAuth client entitlement, not a quality setting | `sqlite3 data/streamrip.db "SELECT value FROM config WHERE key='tidal_auth_method'"` returns `device_code` | Run the PKCE flow in Settings (section 8) |
| 9 | Scan progress stuck at `0 / ?`; or 409 "Another scan is already running" | One-at-a-time in-memory job guard + REST polling design | `curl -s http://localhost:8080/api/library/scan-fuzzy/<job_id>` | Section 9 |
| 10 | `database is locked` during scan + download | Dedup DBs opened with bare `sqlite3.connect` (no WAL, default timeout) while the SDK writes the same file | Error path names `downloads.db` / `downloads-tidal.db`, and a download was active | Section 10 |
| 11 | Album stuck `queued`/`downloading` after restart | Download queue is in-memory; nothing resets album statuses on boot | `curl -s http://localhost:8080/api/downloads/queue` is empty while the album shows queued | Section 11 |
| 12 | Tidal PKCE completes with 400 "Unknown or expired PKCE handle" | In-memory `_pkce_pending` dict lost on backend restart — `--reload` file saves count | Did the backend restart between pkce-start and pkce-complete? Check uvicorn reload output | Restart the flow (section 12) |
| 13 | `GET .../albums?status=all` returns rows but `total=0` | `count_albums` filters on any truthy status; `get_albums` special-cases `"all"` | Compare the same request with the `status` param omitted | Section 13 |
| 14 | Vite dev server (`npm run dev`): every API call fails | No proxy in `frontend/vite.config.ts` and no CORS middleware in the backend | `cat frontend/vite.config.ts` — plugins only, no `server.proxy` | Use `make dev` instead (section 14) |
| 15 | grep finds code that does not exist in the project | `.claude/worktrees/agent-*/` hold full copies of the deleted CLI-era codebase | Does the match path start with `.claude/worktrees/`? | Use `git grep` (section 15) |
| 16 | Download fails immediately with a ValueError about `int()` | Non-numeric `qobuz_quality`/`tidal_quality` config value — `int(q_val)` in `_download_album` raises | `sqlite3 data/streamrip.db "SELECT key,value FROM config WHERE key LIKE '%quality'"` | Set `qobuz_quality` to 1–4 or `tidal_quality` to 0–4; scales in `libsync-config-and-flags` |
| 17 | Album marked failed with "below 80% threshold" | Hardcoded 0.8 success-rate literal in `_download_album` (`backend/services/download.py`) | Count per-track failures in the queue item's track statuses | Investigate the per-track failures first; the threshold itself is code, not config |

## 1. Library loads but downloads fail with 401

**Mechanism.** Qobuz has two app identities with different capabilities (`304027809` OAuth, `798273057` web-player), and the `X-App-Id` header must match the app that issued the token. Per commit `6650993` (2026-04-09): "Qobuz ignores X-App-Id on favorites/* but validates it on album/* and the downloader — hence get_albums succeeds while get_album_with_tracks and downloads return 401." That claim is documented in commit history and code comments; it describes live Qobuz behavior that this repo cannot re-verify offline. Separately, downloads sign `track/getFileUrl` with an app secret; browsing never uses it.

**Critical nuance**: the library grid reads local SQLite (`GET /api/library/{source}/albums` → `LibraryService.get_albums` → `db.get_albums`), so "the library loads" proves nothing about the token. Use live-API calls to discriminate.

**Discriminating experiment.** Run these three probes in order against your running instance:

```bash
# Probe A — favorites endpoint (X-App-Id NOT validated here):
curl -s -X POST http://localhost:8080/api/library/refresh/qobuz | head -c 300

# Probe B — catalog search (live API):
curl -s "http://localhost:8080/api/library/search/qobuz?q=test" | head -c 300

# Probe C — check which app identity is cached:
sqlite3 data/streamrip.db "SELECT key, value FROM config WHERE key IN ('qobuz_app_id','qobuz_app_secret')"
```

| Probe A + B result | Downloads | Diagnosis |
|--------------------|-----------|-----------|
| Both 401/error | 401 | **Token expired.** Comments in `backend/main.py` (`_init_clients`) and `backend/api/auth.py` state Qobuz returns 401 on every endpoint once the token is bad. There is no refresh endpoint — re-run the OAuth flow in Settings (CLAUDE.md Known Issues). |
| Both succeed | 401 on album detail / download start | **App-id mismatch.** An OAuth-issued token must pair with `qobuz_app_id=304027809`; a manually pasted web-player token with `798273057` (auto-pinned by `backend/api/config.py` when `qobuz_token` changes without an explicit app_id). Compare probe C against how the token was obtained. |
| Both succeed, album detail succeeds | Only the download itself fails at `track/getFileUrl` | **Bad signing secret.** `get_file_url` in the SDK's `qobuz/streaming.py` is the only MD5-signed call in the download path. The exact HTTP status Qobuz returns for a bad signature is unverified here. Check the resolution chain below. |

**Secret resolution chain** (`_resolve_qobuz_credentials` in `backend/main.py`): (1) user override config key `qobuz_app_secret`; (2) hardcoded `qobuz.auth.APP_SECRET` when app_id equals the OAuth app; (3) spoofer scrape of the play.qobuz.com bundle, verified against the current app_id, falling back to the first candidate with a warning. A spoofer-scraped secret can never sign for the OAuth app — set `qobuz_app_secret` in Settings in that case.

**Hard rule with incident history**: never unconditionally overwrite the cached `qobuz_app_id`. Commit `8e1f8e2` did exactly that and broke web-player-token users; `cfa4449` reverted it. The full saga lives in `libsync-failure-archaeology`; the step-by-step forensic recipe (header capture, signature reproduction) lives in `libsync-proof-and-analysis-toolkit`; the structural-fix campaign is `libsync-qobuz-auth-campaign`.

## 2. A settings toggle behaves inverted or ignored

**Mechanism.** Pydantic stringifies booleans into the config table as `"True"`/`"False"` (capital first letter). Any code comparing against lowercase `'true'` silently inverts or disables the toggle. This shipped once: commit `c29f2ea` (2026-04-14) fixed inverted `embed_artwork` and `source_subdirectories`.

**Two comparison styles coexist today** (verified 2026-07-03):

- **Case-insensitive (correct)**: `_parse_bool` in `backend/services/download.py` (top of file) accepts `"true"/"1"/"yes"` in any casing. The download config path uses it.
- **Case-SENSITIVE (fragile)**: `backend/api/library.py` computes `sentinel_enabled` twice — in `mark_downloaded` and in `start_scan` — as `(db.get_config("scan_sentinel_write_enabled") or "True") == "True"`. A hand-written lowercase `"true"` in the config table silently disables sentinel writes.

**Discriminating check.**

```bash
sqlite3 data/streamrip.db "SELECT key, value FROM config WHERE value IN ('true','false','TRUE','FALSE')"
```

Any rows returned are landmines for the case-sensitive sites. Values written through the Settings UI are `"True"`/`"False"` and are safe.

**Fix.** Normalize the stored value to `"True"`/`"False"` casing (via the Settings UI or a `PATCH /api/config`). When writing new backend code that reads a boolean config key, always use `_parse_bool` — its docstring exists precisely because of this bug class. Migrating the two case-sensitive sites in `backend/api/library.py` to `_parse_bool` is an open candidate fix; route it through a PR per `libsync-change-control`.

## 3. My `logger.info` never prints

**Mechanism.** All backend modules log via `logging.getLogger("streamrip")`, and there is no `basicConfig`/`dictConfig` anywhere in `backend/` (verified by grep, 2026-07-03). Uvicorn's default logging config only configures the `uvicorn*` loggers, so the `streamrip` logger has no handler and an effective level of WARNING in every runtime mode: `make dev`, `make dev-backend`, and Docker (the Dockerfile CMD passes no `--log-config`). WARNING and above leak out through Python's last-resort handler as bare messages with no timestamp.

**The classic mislead**: under pytest the logs DO appear, because `pyproject.toml` sets `log_cli = true` and `log_level = "DEBUG"`. Do not conclude from a passing test's log output that the same INFO line is visible in production.

**Discriminating check.** Temporarily change the call to `logger.warning(...)`. If the bare message now appears on stderr, logging is unconfigured and your code path was executing all along.

**Fix for a debugging session.** Add `logging.basicConfig(level=logging.INFO)` near the top of `backend/main.py` locally while you debug, and remove it before committing. Making INFO logging permanent is an open design question (quiet-by-default may be intentional — inferred, confirm with maintainer); a permanent change goes through a PR per `libsync-change-control`. For measurement-grade instrumentation, see `libsync-diagnostics-and-tooling`.

## 4. `ModuleNotFoundError: No module named 'qobuz'` or `'tidal'`

**Mechanism.** The SDKs are installed by `make deps` as editable pip installs from the `sdks/qobuz_api_client` submodule (`Makefile` `deps` target: `git submodule update --init --recursive`, then `poetry run pip install -e` on both SDK paths). They are NOT Poetry dependencies — `pyproject.toml` says so in a comment. Therefore anything that rebuilds or syncs the venv (`poetry sync`, `poetry install --sync`, deleting the venv, changing Python versions) silently removes them.

**Discriminating check.**

```bash
poetry run pip list | grep -i -e qobuz -e tidal
ls sdks/qobuz_api_client/clients/python/qobuz/  # empty dir = submodule not initialized
```

**Fix.** Run `make deps`. If the submodule directory is empty (fresh clone without `--recursive`), `make deps` also fixes that. Full environment recreation belongs to `libsync-build-and-env`.

## 5. `make lint` fails on untouched main

**Mechanism.** `make lint` runs `poetry run ruff check backend/` with the locally pinned ruff 0.1.15 (`pyproject.toml` pins `ruff = "^0.1"`), while CI's required `ruff` check uses `chartboost/ruff-action@v1` with a different (newer, version-invisible) ruff. As of 2026-07-03 on clean main, `make lint` exits 1 with exactly 4 RUF100 "Unused noqa directive" errors: `backend/main.py:350`, `backend/main.py:355`, `backend/services/download.py:380`, `backend/services/scan.py:478`. CI passes on the same tree because its ruff recognizes `ASYNC240` as a real rule, so those `noqa` comments are NOT unused there.

**Do NOT run `ruff --fix` with the local 0.1.15** — deleting those noqa comments would break the required CI check. Also note `make lint` only checks `backend/` while CI ruff-checks and format-checks the repo root, so local green is not CI green in either direction.

**Fix.** Treat CI as the authority. Ignore the 4 known RUF100 errors locally; any new error beyond those four is yours. The pre-push checklist that actually matches CI is in `libsync-validation-and-qa`. Bumping the ruff pin to close the skew is an open candidate change — PR only, per `libsync-change-control`.

## 6. UI 404s but the API works

**Mechanism.** `make build-local` (also the first step of `make dev`) runs `rm -rf backend/static && cp -r frontend/build backend/static`. If `npm run build` fails midway, `backend/static/` ends up missing or stale. `create_app` in `backend/main.py` only registers the SPA catch-all route `if os.path.exists(static_dir)` at app-creation time — so the API routers all work while every page URL 404s.

**Discriminating check.** `ls backend/static/index.html`. Missing file confirms it. Also note the check happens at startup: creating `backend/static/` after the server booted does nothing until restart.

**Fix.** Run `make build-local` and read the npm output for the real build error, then restart the backend. If you only changed backend code, `make dev-backend` skips the frontend rebuild and serves the last good copy.

## 7. Downloaded FLAC file is not FLAC / strict scanners choke

**Mechanism.** Tidal PKCE-entitled lossless arrives via DASH as fragmented MP4 with FLAC frames inside. The SDK's `_remux_mp4_to_flac` (in the submodule at `sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py`) uses `ffmpeg -c:a copy` to extract a native FLAC. If ffmpeg is not on PATH, it logs a warning on the `tidal.downloader` logger and **returns False, leaving the file as MP4-wrapped FLAC with a `.flac` name** — the download still reports success. Given section 3, that warning is easy to miss (bare stderr line, no timestamp) though it should leak through Python's last-resort handler (reasoned from the logging setup, not empirically observed).

**Discriminating check.**

```bash
which ffmpeg                      # empty = the cause
file "/path/to/track.flac"        # "ISO Media" = MP4 wrapper; "FLAC audio" = real FLAC
```

Symptoms downstream: `mutagen.flac` raises `FLACNoHeaderError`, strict library scanners reject the file (documented in the v0.0.5 CHANGELOG).

**Fix.** Install ffmpeg (`brew install ffmpeg` on macOS; the Docker image already apt-installs it), then re-download the affected albums with force (the Downloads page retry uses `force: true`, bypassing the dedup DB). Codec/DASH background is in `qobuz-tidal-domain-reference`.

## 8. Tidal downloads capped at AAC ~320 kbps

**Mechanism.** This is an OAuth **client entitlement**, not a quality setting. The SDK's `tidal/auth.py` documents it directly: the device-code client identity "is capped at AAC ~320kbps regardless of the user's subscription"; the PKCE client "unlocks LOSSLESS, HI_RES, HI_RES_LOSSLESS per subscription". Raising `tidal_quality` in Settings cannot exceed the token's entitlement.

**Discriminating check.**

```bash
sqlite3 data/streamrip.db "SELECT value FROM config WHERE key='tidal_auth_method'"
```

`device_code` (or empty — it is the default in `_init_clients`, `backend/main.py`) means the cap applies. `pkce` means look elsewhere: check `tidal_quality` (0–4) and the actual subscription tier — the two caps are independent.

**Fix.** Run the Tidal PKCE flow from Settings (`/api/auth/tidal/pkce-start` then paste the redirect URL into pkce-complete). This sets `tidal_auth_method='pkce'`. Mind section 12 while doing it.

## 9. Scan stuck at `0 / ?` and 409 "Another scan is already running"

**Mechanism.** Fuzzy scans are a one-at-a-time in-memory job: `POST /api/library/scan-fuzzy` returns 409 with `{"error": "Another scan is already running"}` whenever `app.state.active_scan_job` is set (`start_scan` in `backend/api/library.py`). Progress is not pushed over the WebSocket to the UI — the Settings page polls `GET /api/library/scan-fuzzy/{job_id}` every 500 ms, and the endpoint returns `{status, scanned, total}` from an in-memory registry fed by a `_ProgressTrackingBus` wrapper.

**Triage table.**

| Observation | Meaning | Action |
|-------------|---------|--------|
| 409 on start | A scan genuinely holds the guard; it clears in the runner's `finally` even on crash | Wait; poll the running job |
| Polling returns 404 "Job not found" | Backend restarted — jobs and the guard are in-memory only | Start a new scan |
| Stuck at `0 / ?` on a pre-v0.0.4 build | Historical bug: the polling endpoint returned only `{status}` with no counts; fixed in PR #14 (commit `00e2a19`) | Upgrade |
| Stuck at `0 / N` on current code with large N | Scan emits progress per folder; the first folders may be slow (mutagen tag reads) | Watch `scanned` for a minute; if truly frozen, check server stderr for the job's `scan-fuzzy job ... failed` traceback |

**Fix.** Usually just wait or restart the scan. If a job errored, the endpoint returns `{status: "error"}` and the traceback went to `logger.exception` — which IS visible (ERROR level leaks through even unconfigured logging, section 3).

## 10. `database is locked` during scan + download

**Mechanism.** The main app DB uses WAL (write-ahead logging, SQLite's concurrent-writer mode) and a 30 s busy timeout (`backend/models/database.py`, `_connect`). The per-source **dedup DBs do not**: `_populate_dedup` and `_remove_from_dedup` in `backend/services/scan.py` open them with bare `sqlite3.connect(path)` (default 5 s timeout, no WAL), and they do not catch `sqlite3.OperationalError`. Meanwhile, during an active download the SDK downloader writes the same file per completed track (`_mark_downloaded` in the SDK's `qobuz/downloader.py`, also a bare connect — but the SDK side catches and warns). So a scan confirm or a "Mark as downloaded" click during a busy download can hit the lock; the backend side raises and the scan job flips to error.

**Discriminating check.** The error or job failure coincides with an active download, and the traceback references `downloads.db` or `downloads-tidal.db` — not `streamrip.db`.

**Fix (operational).** Do not run scan-fuzzy or mark/unmark-downloaded while downloads are active; retry once the queue drains. **Fix (code, open candidate)**: add `timeout=` and WAL to the dedup connections in `backend/services/scan.py` — PR per `libsync-change-control`.

## 11. Album stuck `queued`/`downloading` after restart

**Mechanism.** The download queue is a plain in-memory list (`self._queue` in `DownloadService`, `backend/services/download.py`), and nothing at boot resets album `download_status` rows (verified: no startup reset code in `backend/main.py`). A restart forgets the queue but leaves the DB rows saying `queued` or `downloading` forever.

**Discriminating check.**

```bash
curl -s http://localhost:8080/api/downloads/queue          # empty
sqlite3 data/streamrip.db "SELECT id, title, download_status FROM albums WHERE download_status IN ('queued','downloading')"
```

Empty queue + populated rows = this failure mode, not a hung download.

**Fix.** Either re-enqueue the album (the download proceeds and overwrites the status), or reset it via `POST /api/library/albums/{id}/unmark-downloaded`, which sets `not_downloaded` through the `unmark_album_downloaded` primitive (its dedup-row deletions are harmless no-ops for a never-downloaded album). Using unmark as a stuck-status reset is inferred from the code path — confirm with maintainer before scripting it in bulk. A boot-time status reset is an open design question.

## 12. Tidal PKCE completes with 400 "Unknown or expired PKCE handle"

**Mechanism.** `pkce-start` stores the PKCE verifier in a module-level in-memory dict `_pkce_pending` in `backend/api/auth.py`, keyed by an opaque handle; `pkce-complete` pops it and returns 400 with detail "Unknown or expired PKCE handle. Start the flow again." if the handle is missing. Any backend restart between the two calls loses the dict — **and `make dev`/`make dev-backend` run uvicorn with `--reload`, so merely saving a backend file mid-login triggers the restart**.

**Discriminating check.** Look at the uvicorn console for a reload/restart line between when you clicked "start" and when you pasted the redirect URL.

**Fix.** Restart the flow and complete it without touching backend files. Persisting pending PKCE state is an open candidate improvement.

## 13. `GET /api/library/{source}/albums?status=all` returns rows but `total=0`

**Mechanism.** In `backend/models/database.py`, `get_albums` skips the status filter with `if status and status != "all"`, but `count_albums` filters with plain `if status:` — so `status=all` counts albums whose literal `download_status` is `'all'` (none). `LibraryService.get_albums` feeds both with the same status value, so the response contains albums alongside a lying `total`. Anything paginating off `total` (Load More logic) misbehaves for that value.

**Discriminating check.** Repeat the request with the `status` query param omitted; `total` becomes correct.

**Fix (operational).** Omit `status` instead of passing `all`. **Fix (code, open candidate)**: mirror the `!= "all"` special case in `count_albums` — small PR, add a regression test per `libsync-validation-and-qa`.

## 14. Vite dev server: all API calls fail

**Mechanism.** The frontend API client hardcodes relative `BASE = '/api'` (`frontend/src/lib/api/client.ts`), `frontend/vite.config.ts` contains no `server.proxy` (verified: plugins only), and the backend registers no CORS middleware (verified: no `add_middleware`/`CORSMiddleware` anywhere in `backend/`). So under `npm run dev` (or `make dev-frontend`) every `/api` fetch and the `/api/ws` WebSocket hit the Vite server on :5173 and fail. The Makefile comment claiming the dev server is "proxied to backend" is stale. Pointing fetches directly at `:8080` would fail on CORS instead — there is no split-origin dev setup that works today.

**Fix.** Use `make dev` (full build + serve from the backend) or `make dev-backend` (backend only, serves the last-built frontend). Adding a Vite proxy is an open candidate change.

## 15. grep finds code that does not exist

**Mechanism.** `.claude/worktrees/agent-*/` directories (gitignored, on-disk) contain complete pre-rebuild source trees including the deleted `streamrip/` CLI and the vendored `qobuz-api/`. Repo-root greps happily match them. Two more traps: case-insensitive `todo` searches match "Cryp**todo**me" in the SDK, and `frontend/.svelte-kit/` plus `frontend/node_modules/` add generated noise.

**Discriminating check.** Look at the match path. `.claude/worktrees/...` = ghost code.

**Fix.** Prefer `git grep <pattern>` — it only searches tracked files, so gitignored worktrees, node_modules, and build output are invisible by construction. If you need plain grep:

```bash
grep -rn "pattern" --exclude-dir={.claude,node_modules,.svelte-kit,sdks,.git} .
```

Include `sdks` deliberately only when you intend to search the SDK submodule.

## When NOT to use this skill

- **Recreating a broken dev environment from scratch** (not just a missing SDK import): `libsync-build-and-env`.
- **Looking up a config key's meaning, default, or precedence**: `libsync-config-and-flags`.
- **The deep Qobuz auth forensic recipe** (capturing headers, reproducing signatures): `libsync-proof-and-analysis-toolkit`; the **history of the app-id saga**: `libsync-failure-archaeology`; the **campaign to fix auth structurally**: `libsync-qobuz-auth-campaign`.
- **Adding instrumentation or measuring instead of eyeballing**: `libsync-diagnostics-and-tooling`.
- **Deciding what evidence gates a push/PR** after you have a fix: `libsync-validation-and-qa` and `libsync-change-control`.
- **Understanding why the architecture is shaped this way** (invariants, known-weak points): `libsync-architecture-contract`.
- **Streaming-domain theory** (codecs, OAuth variants, DASH, signing model): `qobuz-tidal-domain-reference`.
- **Running, deploying, releasing, or deciding what data files are safe to delete**: `libsync-run-and-operate`.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it; all commands are read-only.

- `_parse_bool` exists and is case-insensitive: `grep -n "_parse_bool" backend/services/download.py`
- Case-sensitive sentinel comparisons still present: `grep -n 'scan_sentinel_write_enabled' backend/api/library.py` (expect two `== "True"` sites)
- No logging config in backend: `grep -rn "basicConfig\|dictConfig" backend/` (expect no matches)
- pytest log settings that mislead: `grep -n "log_cli\|log_level" pyproject.toml`
- SDKs still editable-pip, not Poetry deps: `sed -n '/^deps:/,+3p' Makefile && poetry run pip list | grep -i -e qobuz -e tidal`
- Local ruff version and the 4 known RUF100 errors: `poetry run ruff --version && poetry run ruff check backend/` (as of 2026-07-03: 0.1.15; main.py:350, main.py:355, download.py:380, scan.py:478)
- Static dir conditional mount: `grep -n "os.path.exists(static_dir)" backend/main.py`
- ffmpeg remux fallback returns False: `grep -n "_remux_mp4_to_flac\|ffmpeg not on PATH" sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py`
- Device-code AAC cap vs PKCE entitlement: `sed -n '1,12p' sdks/qobuz_api_client/clients/python/tidal/tidal/auth.py`
- Scan job guard and 409 message: `grep -n "active_scan_job\|Another scan is already running" backend/api/library.py backend/main.py`
- Dedup DBs opened without WAL/timeout: `grep -n "sqlite3.connect" backend/services/scan.py` (bare calls) vs `grep -n "timeout=30" backend/models/database.py`
- No boot-time status reset: `grep -rn "queued" backend/main.py` (expect no matches)
- PKCE in-memory dict and error message: `grep -n "_pkce_pending\|Unknown or expired" backend/api/auth.py`
- status=all count asymmetry: `grep -n 'status != "all"' backend/models/database.py` (present in get_albums, absent in count_albums)
- No Vite proxy, no CORS: `cat frontend/vite.config.ts && grep -rn "CORSMiddleware" backend/` (expect no CORS matches)
- Ghost worktrees on disk: `ls .claude/worktrees/ 2>/dev/null`
- Qobuz app identities and secret chain: `grep -rn "304027809\|798273057" backend/main.py backend/api/config.py backend/api/auth.py`
- App-id saga commits still resolvable: `git log --oneline --no-walk 6650993 8e1f8e2 cfa4449 e4e0a1f c29f2ea 00e2a19`
- Test baseline: `make test` (as of 2026-07-03, v0.0.6: 154 passed)
