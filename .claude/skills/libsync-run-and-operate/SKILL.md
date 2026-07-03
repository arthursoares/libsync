---
name: libsync-run-and-operate
description: Use when running libsync locally or in Docker, choosing a compose file, deciding whether streamrip.db, downloads.db, .streamrip.json, or other data files are safe to delete, cutting a release or pushing a version tag, or diagnosing symptoms like logger.info output never appearing, auto-sync not running after being enabled, the download queue vanishing after a restart, albums stuck in queued/downloading, "Unknown or expired PKCE handle", or no versioned GHCR image after tagging.
---

# libsync: run, deploy, release, and operate

Everything here was verified against the repo as of 2026-07-03 (v0.0.6, main). Legacy `streamrip` names (env vars, DB filenames, logger, Docker volume) are intentional until the v1.0 rename — do not "fix" them (see CLAUDE.md).

## 1. Run modes

| Mode | Command | What it does |
|---|---|---|
| Full local dev | `make dev` | Builds frontend, replaces `backend/static/`, then runs uvicorn on :8080 with `--reload` |
| Backend-only dev | `make dev-backend` | Same uvicorn invocation, skips the frontend rebuild (serves whatever is in `backend/static/`) |
| Docker (Makefile) | `make docker` | `docker build -f docker/Dockerfile -t streamrip .` then `docker run -p 8080:8080 -v ~/Downloads/Music:/music -v streamrip-data:/data -e STREAMRIP_DB_PATH=/data/streamrip.db streamrip` |
| Compose, build from source | `docker compose -f docker/docker-compose.yml up` | Builds from repo context; named volume `streamrip-data:/data`, bind mount `/mnt/music:/music` |
| Compose, published image | `docker compose -f docker-compose.example.yml up` | Pulls `ghcr.io/arthursoares/libsync:latest`; bind mounts `./music:/music` and `./data:/data` |

Details that matter:

- Both `make dev` and `make dev-backend` run `mkdir -p data` and set exactly two environment variables: `STREAMRIP_DB_PATH=data/streamrip.db` and `STREAMRIP_DOWNLOADS_PATH=~/Downloads/Music`. These are the only env vars the backend reads (`grep -rn os.environ backend/`). The DB config key `downloads_path`, when set in Settings, overrides the env var.
- The uvicorn invocation is `uvicorn backend.main:create_app --factory --port 8080 --reload`. "Factory" means uvicorn calls `create_app()` to build the app; `--reload` restarts the process on any file save — which also wipes all in-memory state (see section 3).
- The Docker image (docker/Dockerfile) is a two-stage build: node:20-alpine builds the frontend into `backend/static/`; python:3.12-slim installs ffmpeg via apt, `poetry install --only main`, then pip-installs the two SDK packages copied from the host's submodule checkout. `EXPOSE 8080`; `CMD ["uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]`.
- Run `git submodule update --init --recursive` before any `docker build`. The Dockerfile COPYs `sdks/qobuz_api_client/clients/python/` from the host; an uninitialized submodule silently copies an empty directory and produces a broken image.
- Choose the compose file by intent: use `docker/docker-compose.yml` when developing image changes (builds from your checkout, state in a named Docker volume); use `docker-compose.example.yml` for deployments (pulls the published GHCR image, state in host-visible `./data` and `./music` bind mounts you can back up directly).
- `make dev-frontend` (Vite dev server on :5173) has no working API path: the API client hardcodes relative `/api` URLs and `frontend/vite.config.ts` defines no proxy, and the backend registers no CORS middleware. Use `make dev` instead.

## 2. Data-artifact map

All state lives in the directory containing `STREAMRIP_DB_PATH` (`data/` locally, `/data` in Docker). The dedup DB directory is derived from the DB path, not from the downloads path (backend/services/download.py, `_download_album`).

| Artifact | Written by | Safe to delete? |
|---|---|---|
| `streamrip.db` + `-wal`/`-shm` sidecars | backend/models/database.py (SQLite in WAL mode — Write-Ahead Logging, hence the sidecar files) | **NO — destructive.** Holds albums, tracks, sync_runs, AND the `config` table containing all credentials (Qobuz token/secret, Tidal access+refresh tokens). Deleting it loses your logins, not just library state. Back up the whole `/data` directory. |
| `downloads.db` | Download service + `mark_album_downloaded` (backend/services/scan.py) | Yes. Qobuz dedup DB only (single table of already-downloaded track IDs). Deleting loses skip-already-downloaded state; force re-download bypasses it anyway. NOT cleared by `POST /api/config/reset`. |
| `downloads-tidal.db` | Same code path, source-keyed as `downloads-{source}.db` | Yes — same semantics as `downloads.db`, for Tidal. |
| `.streamrip.json` (one per album folder under the music root) | Both SDK downloaders and `mark_album_downloaded` | Yes — regenerable. It is a "sentinel": the fuzzy scan skips any folder containing one (backend/services/scan.py, `run_scan`), so deleting it only makes the next scan re-examine that folder. |
| `data/downloads_albums.db`, `data/failed.db` | Nothing — legacy files from the removed streamrip CLI. `grep -rn "downloads_albums\|failed.db" backend tests docs` returns no hits. | Yes — zombie files, referenced by no code. |
| `captures/*.har` | Manual HAR captures (browser/proxy recordings of HTTP traffic) | Delete freely, but **never commit or share them** — the .gitignore comment states they contain plaintext auth material (real tokens). They are gitignored via `captures/` and `*.har`. |

Stray `*.db` files at the repo root (from old runs) are gitignored and referenced by no code; the canonical location is the `data/` directory.

## 3. Operational behaviors that surprise people

- **Auto-sync sleeps a full interval before its first run.** The loop in backend/services/sync.py (`start_auto_sync`) is `while True: await asyncio.sleep(interval_seconds); run_sync(...)`. Enabling auto-sync does not sync immediately — trigger a manual sync if you want one now. Default interval is 6h; presets 1h/6h/12h/daily/24h or a bare integer of seconds (min 60).
- **Auto-sync is single-source.** `_start_auto_sync_if_enabled` (backend/main.py) picks the first connected source in the order `("qobuz", "tidal")`. With both connected, only Qobuz auto-syncs.
- **A restart forgets the download queue and strands statuses.** The queue is an in-memory list (`DownloadService._queue`). Album rows set to `queued` or `downloading` in `streamrip.db` are never reset at boot — no startup code touches `download_status` (verified: no `update_album_status` call in backend/main.py). This is a known gap; after a restart, stuck albums must be re-enqueued (or the row fixed by hand — back up the DB first).
- **`POST /api/config/reset` is library-only.** It deletes rows from `albums`, `tracks`, and `sync_runs`; it preserves the `config` table (credentials) and touches no files. It does NOT clear the dedup DBs or sentinels, so previously downloaded albums are still skipped unless you force.
- **Cancel is soft.** `DownloadService.cancel` only adds the item ID to a set and flips statuses; it does not abort the in-flight SDK download. A cancel issued mid-album lets the current download finish writing files to disk; the item is then marked cancelled.
- **A restart mid-Tidal-PKCE-login kills the flow.** PKCE (Proof Key for Code Exchange, the Tidal OAuth variant used by "Connect Tidal" via URL paste) stores its verifier in a module-level in-memory dict (`_pkce_pending`, backend/api/auth.py). Any restart — including `--reload` picking up a file save — makes `pkce-complete` return 400 "Unknown or expired PKCE handle. Start the flow again." Restart the login from Settings.

## 4. Logging reality

There is no logging configuration anywhere in `backend/` (no `basicConfig`, `dictConfig`, or `addHandler`), and no runtime mode passes `--log-config` or `--log-level` to uvicorn. Consequences:

- `logger.info(...)` from the `streamrip` logger is invisible in `make dev`, `make dev-backend`, and Docker. Only WARNING+ leaks out via Python's `lastResort` handler, with no timestamps. No log file is ever written (`*.log` is gitignored but nothing creates one).
- pytest DOES show INFO/DEBUG (`log_cli = true`, `log_level = "DEBUG"` in pyproject.toml) — do not let test output convince you runtime logging works.

To actually see backend logs, use the maintained config shipped with the diagnostics skill: run uvicorn with `--log-config .claude/skills/libsync-diagnostics-and-tooling/scripts/logging-config.json` (it scopes the `streamrip` and uvicorn loggers to INFO with timestamps — see that skill's "Step 0" for the full command). Alternatively, temporarily add `logging.basicConfig(level=logging.INFO)` near the top of backend/main.py — remove it before committing; a permanent logging config is an open design question for the maintainer.

## 5. Release runbook

Verified from git history, tag objects, and `.github/workflows/pytest.yml` (as of 2026-07-03, v0.0.6). The git tag is the only real version — pyproject.toml says `version = "3.0.0"` and frontend/package.json says `"0.0.1"`; both are meaningless leftovers. Do not bump them.

1. Land a CHANGELOG entry on `main` through the normal PR flow (required CI checks: backend-tests, frontend-build, docker-publish, ruff). Commit subject pattern observed for every release: `docs(release): CHANGELOG entry for vX.Y.Z` (v0.0.6 = ba49a77). CHANGELOG heading format: `## vX.Y.Z — YYYY-MM-DD`.
2. Create an **annotated** tag on that commit: `git tag -a v0.0.7 -m "v0.0.7 — 2026-07-03"`. Existing tags (v0.0.5, v0.0.6) are annotated tag objects with exactly this message shape.
3. Push the tag: `git push origin v0.0.7`. The tag push (matching `on.push.tags: ['v*']`) triggers pytest.yml; its `docker-publish` job (needs backend-tests + frontend-build) publishes GHCR images via docker/metadata-action semver patterns (`{{version}}`, `{{major}}.{{minor}}`, `{{major}}`) plus `:latest` (flavor `latest=` is true only on `refs/tags/v*`). Merging to main alone does NOT publish version-tagged images — the tag push does.

Warnings:

- **Use 3-part semver tags only.** The 4-part tag `v0.0.5.1` exists as precedent and is not valid semver, so metadata-action's semver patterns generate no versioned image tags for it (inferred from docker/metadata-action semver semantics; the workflow config is verified, the registry outcome was not re-checked).
- **GHCR images are amd64-only on main.** The docker-publish job on main has no `platforms:` key; a multi-arch version (`platforms: linux/amd64,linux/arm64`) exists only on `origin/dev` and is stranded there. ARM hosts (e.g. Raspberry Pi) cannot run the published image natively.
- The submodule is checked out with `submodules: recursive` in CI, so the image bakes in the committed SDK pin — but a **local** `docker build` bakes in whatever the host submodule working tree contains, including uncommitted edits.

## 6. Deployment safety

- **There is no authentication.** No middleware is registered in backend/main.py and no route uses an auth dependency (verified by grep for `Depends`/`Security`/`CORSMiddleware`). Worse, `GET /api/config` returns `qobuz_token`, `qobuz_app_secret`, and `tidal_access_token` in plaintext. Never expose :8080 to the public internet. Run it on a trusted LAN or behind a reverse proxy that adds authentication — that placement IS the security model.
- **The UI depends on external CDNs.** `frontend/src/app.html` loads the Atkinson Hyperlegible Next font from fonts.googleapis.com, and album cover art is hotlinked directly from Qobuz image hosts and `resources.tidal.com` (backend/services/library.py builds the URLs; the browser fetches them). Offline or air-gapped deployments run, but show no cover art and fall back to system fonts, and browsers leak requests to those CDNs.
- Back up the whole `/data` directory (or `data/` locally) before upgrades or experiments — it is the single point of loss for credentials, library, and dedup state.

## When NOT to use this skill

- Recreating the dev environment from scratch, `make deps`/submodule/Poetry/Node traps, or build failures → **libsync-build-and-env**.
- A specific runtime failure you need to triage (symptom → experiment tables) → **libsync-debugging-playbook**.
- Config key semantics, defaults, and precedence beyond the two env vars mentioned here → **libsync-config-and-flags**.
- How to classify and gate a change, PR/CI requirements in depth → **libsync-change-control**.
- Pre-push test/QA checklist and what counts as evidence → **libsync-validation-and-qa**.
- Qobuz/Tidal auth theory (OAuth flows, app IDs, signing) → **qobuz-tidal-domain-reference**; live Qobuz auth work → **libsync-qobuz-auth-campaign**.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it:

- Makefile targets, env vars, uvicorn flags: `cat Makefile`
- Docker CMD, EXPOSE, submodule COPY: `cat docker/Dockerfile`
- Compose file differences: `cat docker/docker-compose.yml docker-compose.example.yml`
- Only two env vars: `grep -rn "os.environ" backend --include='*.py'`
- Auto-sync sleeps first / single-source: `grep -n -A4 "_auto_sync_loop" backend/services/sync.py` and `grep -n -B2 -A8 "Pick the first available source" backend/main.py`
- No boot-time status reset: `grep -n "update_album_status\|download_status" backend/main.py` (expect no hits)
- Soft cancel: `grep -n -A10 "async def cancel" backend/services/download.py`
- Reset semantics: `grep -n -A10 "def reset_database" backend/api/config.py`
- PKCE in-memory dict and error text: `grep -n "_pkce_pending\|Unknown or expired" backend/api/auth.py`
- No logging config: `grep -rn "basicConfig\|dictConfig\|addHandler" backend --include='*.py'` (expect no hits)
- Dedup DB naming and force bypass: `grep -n "downloads-\|downloads_db = None" backend/services/download.py`
- Sentinel short-circuit: `grep -n '".streamrip.json"' backend/services/scan.py`
- Zombie DB files unreferenced: `grep -rn "downloads_albums\|failed.db" backend tests docs` (expect no hits)
- HAR danger note: `grep -n -B1 -A2 "captures" .gitignore`
- Release trigger, semver tags, latest flavor, platforms: `sed -n '1,10p;91,150p' .github/workflows/pytest.yml` and `grep -n platforms .github/workflows/pytest.yml` (no hit on main = amd64-only)
- Tag style and commit pattern: `git tag -n3` and `git log --format='%s' -1 v0.0.6^{commit}`
- Version strings are meaningless: `grep -n '^version' pyproject.toml; grep -n '"version"' frontend/package.json`
- No auth / credential exposure: `grep -rn "Depends\|Security\|CORSMiddleware" backend/main.py backend/api/` and `grep -n -A6 "class AppConfig" backend/models/schemas.py`
- External CDNs: `grep -n "fonts.googleapis" frontend/src/app.html; grep -n "resources.tidal.com" backend/services/library.py`
