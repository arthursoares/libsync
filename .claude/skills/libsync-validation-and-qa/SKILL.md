---
name: libsync-validation-and-qa
description: Use when preparing to push or open a PR in libsync, deciding which checks to run before merge, adding or locating tests, or judging whether a fix has enough evidence. Also use on these symptoms — make lint fails with RUF100 errors on a clean main checkout, local ruff results differ from CI, node --test reports a single failing "tests" entry, a required check (backend-tests, frontend-build, docker-publish, ruff) blocks a merge, or a change is claimed to "work on my machine" without a test.
---

# libsync validation and QA

This skill defines what counts as evidence in this repo, inventories the test suite, and gives the pre-push checklist that actually matches what CI enforces. All facts below were re-verified against the repo on 2026-07-03 at v0.0.6 (main = ba49a77) unless labeled otherwise.

## 1. Test inventory

Backend tests live in the top-level `tests/` directory (not `backend/tests/`). There is no `conftest.py`; every file defines its own fixtures. The suite is 22 files / 154 tests, needs no credentials and no network, and finishes fast (measured 2026-07-03: "154 passed in 2.14s", about 3 seconds wall through `make test`). CLAUDE.md's "~1.5s" is a slight understatement; treat anything under ~10s wall as normal.

Run it with:

```bash
make test        # poetry run pytest tests/ -q --tb=short
```

| Test file | Tests | Subject |
|---|---|---|
| `tests/test_api_routes.py` | 15 | Route contracts (albums, queue, config round-trip, auth status, sync status/history, OAuth URL) via httpx `ASGITransport` |
| `tests/test_auto_sync.py` | 15 | `SyncService` scheduler: `start_auto_sync` / `stop_auto_sync` / `_auto_sync_loop` |
| `tests/test_app_database.py` | 13 | `AppDatabase` schema and CRUD (`backend/models/database.py`) |
| `tests/test_scan_normalize.py` | 12 | Normalizer for fuzzy artist/album matching (`backend/services/scan.py`) |
| `tests/test_api_library_scan.py` | 11 | `/scan-fuzzy`, `/mark-downloaded`, `/unmark-downloaded` API |
| `tests/test_download_config_bools.py` | 10 | Boolean config values honored regardless of casing (Pydantic "True"/"False" regression) |
| `tests/test_download_service.py` | 8 | `DownloadService` queue: enqueue, get_queue, cancel |
| `tests/test_download_failure_threshold.py` | 7 | The 80% success-rate threshold in `DownloadService._download_album` |
| `tests/test_scan_mark.py` | 7 | `mark_album_downloaded` / `unmark_album_downloaded` primitives |
| `tests/test_scan_matcher.py` | 7 | Fuzzy-match classifier: folder metadata to library album candidates |
| `tests/test_force_redownload.py` | 6 | `force=True` flag propagation into the SDK `DownloadConfig` |
| `tests/test_database_migration.py` | 6 | Schema v1 to v2 migration for the `albums` table |
| `tests/test_scan_job.py` | 6 | End-to-end scan job over a synthetic music folder |
| `tests/test_library_sdk.py` | 5 | `LibraryService` with mocked SDK client objects |
| `tests/test_event_bus.py` | 4 | In-process `EventBus` |
| `tests/test_client_init.py` | 4 | Client initialization and hot-reload |
| `tests/test_scan_tag_reader.py` | 4 | Tag/folder-name metadata extraction |
| `tests/test_static_serving.py` | 4 | SPA static-file catch-all route in `backend/main.py` |
| `tests/test_library_service.py` | 3 | `LibraryService` |
| `tests/test_sync_service.py` | 3 | `SyncService` diff/run/history |
| `tests/test_app_startup.py` | 2 | `create_app` smoke test |
| `tests/test_auto_sync_hotreload.py` | 2 | Auto-sync re-evaluated after credential hot-reload |

Frontend tests are two `node:test` files with 6 tests total: `frontend/tests/api-error-message.test.js` and `frontend/tests/auth-ui-logic.test.js`, covering the plain-JS modules `frontend/src/lib/api/error-message.js` and `frontend/src/lib/auth-ui-logic.js`. They are NOT wired into `frontend/package.json` (no `test` script) and NOT run by any CI job — they pass only if someone runs them manually.

SDK-level tests are not in this repo's suite; they live in the submodule under `sdks/qobuz_api_client/clients/python/{tests,tidal/tests}/`.

### What is NOT covered (as of 2026-07-03, v0.0.6)

| Gap | Evidence |
|---|---|
| `backend/api/websocket.py` has zero tests | `grep -rln -i 'websocket\|/ws' tests/` returns nothing — the only backend module with no coverage |
| No coverage measurement | No `pytest-cov` or coverage config in `pyproject.toml` — "154 passing" says nothing about line coverage |
| No frontend component or E2E tests | No vitest, playwright, eslint, or prettier in `frontend/package.json`; the only frontend type gate is `svelte-check`, which CI does not run |
| The `e2e-tests` CI job is dead | `.github/workflows/pytest.yml` job `e2e-tests` runs only on push to the `dev` branch and targets `tests/test_e2e_download.py`, which does not exist. The unit-test step's `--ignore=tests/test_e2e_download.py -k "not test_streamrip_versions_match"` flags reference the same removed file and a test name that matches nothing — stale but harmless |

## 2. Pre-push checklist (matches what CI actually enforces)

Run these four steps from the repo root before every push. `make lint` alone is NOT sufficient and is currently misleading — see the ruff trap below.

```bash
# 1. Backend tests (required check: backend-tests)
make test

# 2. Ruff, matching CI scope (required check: ruff) — see trap below
poetry run ruff check backend/ tests/
poetry run ruff format --check backend/ tests/

# 3. Svelte type check (NOT CI-enforced, but the only type gate that exists)
cd frontend && npm run check

# 4. Frontend unit tests (NOT CI-enforced; the glob quoting is REQUIRED)
cd frontend && node --test 'tests/*.test.js'
```

### The ruff trap: local lint disagrees with CI

Ruff is the Python linter/formatter. Three facts, all verified 2026-07-03 on clean main:

- CI's `ruff` job (`.github/workflows/ruff.yml`) runs `chartboost/ruff-action@v1` twice — plain `check`, then `format --check` — over the repo root (backend/ AND tests/; the checkout has no submodules, so SDK code is never linted). It PASSES on main (latest run: success, 2026-04-27, run 24995756803). The action's bundled ruff version is not visible in the workflow file.
- `make lint` runs `poetry run ruff check backend/` only — it skips `tests/` entirely — using the locally pinned ruff 0.1.15 (`ruff = "^0.1"` in `pyproject.toml`). It FAILS with exit 1 on a clean main checkout: 4 `RUF100` unused-`noqa` false positives (`backend/main.py:350`, `backend/main.py:355`, `backend/services/download.py:380`, `backend/services/scan.py:478` — line numbers as of 2026-07-03) that CI's newer ruff does not report.
- `poetry run ruff format --check backend/ tests/` with the pinned 0.1.15 wants to reformat 4 test files (`test_auto_sync.py`, `test_auto_sync_hotreload.py`, `test_force_redownload.py`, `test_static_serving.py`) that CI considers already formatted.

Rules that follow:

1. Never run `ruff check --fix` or blanket `ruff format` with the pinned 0.1.15. The "fixes" it proposes (deleting the `noqa: ASYNC240` comments, reformatting those 4 test files) would likely make CI's ruff job fail, because CI's version treats `ASYNC240` as a real rule.
2. When `poetry run ruff check backend/ tests/` reports exactly those 4 known RUF100 errors and nothing else, your tree is clean by CI's standard. Any NEW finding on a file you touched is real — fix it by hand.
3. CI is the arbiter. If local and CI ruff disagree on a file you changed, trust CI and adjust manually.

### Frontend check traps

- `npm run check` runs `svelte-check` (the Svelte/TypeScript type checker). No CI job runs it, so a type error that `vite build` tolerates merges cleanly — running it locally is the only type gate the project has.
- On Node 24 (verified on v24.14.1), `node --test tests/` from `frontend/` FAILS with a single confusing failing entry named "tests". You must pass the quoted glob: `node --test 'tests/*.test.js'`.

### CI-vs-local delta summary

| Check | Local command | CI-enforced? |
|---|---|---|
| Backend tests | `make test` | Yes (`backend-tests`) |
| Ruff lint + format | `poetry run ruff check backend/ tests/` + `ruff format --check backend/ tests/` (NOT `make lint`) | Yes (`ruff`) — but different ruff version than local pin |
| Frontend builds | `cd frontend && npm run build` | Yes (`frontend-build`) |
| Docker image builds | `docker build -f docker/Dockerfile .` (do not run casually; slow) | Yes (`docker-publish`) |
| Svelte type check | `cd frontend && npm run check` | No |
| Frontend unit tests | `cd frontend && node --test 'tests/*.test.js'` | No |

Branch protection on `main` (verified via GitHub API 2026-07-03): required status checks `backend-tests`, `frontend-build`, `docker-publish`, `ruff`; strict mode (branch must be up to date with main before merge); linear history required; conversation resolution required; 0 approving reviews required; CodeQL is NOT required. Do not attempt to route around these gates — see the libsync-change-control skill.

## 3. Evidence standards

What counts as proof that a change works, in descending order of preference:

1. **A failing-then-passing test in `tests/`.** Every bugfix should first reproduce the bug as a failing test, then pass after the fix. Several existing files are explicit regression tests with the regression documented in the module docstring (`test_download_config_bools.py`, `test_auto_sync_hotreload.py`, `test_download_failure_threshold.py`) — follow that convention.
2. **A route-contract test** for API behavior. Pattern (see `tests/test_api_routes.py`): build the app with `create_app(db_path=":memory:")`, wrap it in `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")`, seed data through `app.state.db`, and assert on JSON responses. No live server, no ports.
3. **A service-level test** with a throwaway database. Pattern (see `tests/test_download_service.py`): create `AppDatabase(path)` over a `tempfile.NamedTemporaryFile(suffix=".db")`, unlink in fixture teardown. Note the two patterns differ: API tests use `:memory:` via `create_app`; service tests use temp-file DBs — copy whichever matches your layer.
4. **For UI claims: a screenshot or a written manual-steps record.** There is no E2E harness, no component tests, and no browser automation in the repo (known gap; the manual commands are documented in `docs/WEB_UI.md`, "Testing" section). A UI change with neither a screenshot nor reproducible manual steps is unverified.

"It works on my machine" is NOT evidence here, for two concrete reasons:

- Backend `INFO` logging is invisible at runtime — the app configures no logging handler, so a "look, the log says it worked" claim cannot even be produced outside pytest (pytest shows logs because `log_cli = true` in `pyproject.toml`).
- The local lint/format toolchain disagrees with CI (see the ruff trap above), so a locally green `make lint` proves nothing about the required `ruff` check.

## 4. Acceptance thresholds and pytest behavior

- **80% album-success threshold.** In `backend/services/download.py` (`_download_album`), after the SDK finishes an album: `if result.total > 0 and result.success_rate < 0.8: raise RuntimeError(...)`. Below 80% of tracks succeeding, the download is treated as failed and the album's `download_status` reverts to `not_downloaded` (via `db.update_album_status(..., "not_downloaded")` in the failure paths). Consequence: `get_recent_downloads` in `backend/models/database.py` filters `download_status IN ('complete', 'failed')`, but the pipeline never persists `'failed'` on an album — so the `failed` branch of download history is effectively unreachable from the download pipeline. The threshold behavior is pinned by `tests/test_download_failure_threshold.py`.
- **Pytest config** (`[tool.pytest.ini_options]` in `pyproject.toml`): `addopts = "-ra -q"`, `testpaths = ["tests"]`, `asyncio_mode = "auto"`, `log_cli = true`, `log_level = "DEBUG"`. Two consequences: (a) `async def test_*` functions run without any `@pytest.mark.asyncio` decorator; (b) `make test-unit` (`pytest -v`) output is flooded with per-test asyncio DEBUG log blocks — this is configured noise, not a failure symptom.
- Poetry prints a `poetry.dev-dependencies` deprecation warning on every run — also noise, not a failure.

## 5. How to add a test

1. Create `tests/test_<subject>.py` at the repo root (there is no `backend/tests/`). No `conftest.py` exists; define fixtures in your file, following the closest existing file for your layer.
2. Write `async def test_*` directly — `asyncio_mode = "auto"` handles the event loop. Use plain `def` for pure-sync units (see `tests/test_scan_normalize.py`).
3. Pick the DB fixture by layer: `create_app(db_path=":memory:")` for route tests, `AppDatabase` over a temp file for service tests (patterns in section 3).
4. **Mock the SDK clients — never call live Qobuz/Tidal APIs in tests.** The established patterns: a `MagicMock()` client with `client.catalog` / `client.favorites` attributes and `AsyncMock` methods returning small fake result objects (see `tests/test_download_failure_threshold.py`, classes `FakeTrackResult` / `FakeAlbumResult`, and its `unittest.mock.patch` of the SDK `AlbumDownloader`); or dataclass stand-ins for SDK response types (see `tests/test_library_sdk.py`).
5. Add a module docstring stating what the file covers; several files use a "Test Plan / Scenario: Given/When/Then" docstring format (see `tests/test_download_service.py`) — following it is house style observable in the code (inferred — confirm with maintainer whether it is mandatory for new files).
6. Frontend logic that needs testing should be extracted from `.svelte` files into plain `.js` modules and tested with `node:test` in `frontend/tests/*.test.js` — that is the existing pattern (`auth-ui-logic.js`, `error-message.js`). Remember these do not run in CI; say so in your PR if they are your only evidence.
7. Run `make test` and confirm the count increased and everything passes before pushing.

## 6. CI topology

Three workflow files in `.github/workflows/` (as of 2026-07-03, v0.0.6):

| Workflow file | Jobs | What runs |
|---|---|---|
| `pytest.yml` ("Tests") | `backend-tests` | Checkout with `submodules: recursive`, Python 3.12, Poetry 1.8.0, `poetry install`, pip-install both SDK paths from the submodule, then `pytest tests/ -q --tb=short` plus the stale `--ignore`/`-k` flags (harmless) |
| | `e2e-tests` | DEAD: only triggers on push to the `dev` branch, gated on a `QOBUZ_TOKEN` secret, targets nonexistent `tests/test_e2e_download.py` |
| | `frontend-build` | Node 20, `npm ci`, `npm run build` — nothing else; no type check, no tests |
| | `docker-publish` | Needs `backend-tests` + `frontend-build`; builds `docker/Dockerfile`; pushes to `ghcr.io/<repo>` for same-repo events (fork PRs are build-only); tags branch/`pr-N`/semver/sha; `latest` only on `v*` tags |
| `ruff.yml` ("Ruff") | `ruff` | `chartboost/ruff-action@v1` twice: `check`, then `format --check`, over backend/ + tests/ (no submodules in checkout) |
| `codeql-analysis.yml` | CodeQL | Python-only scan on push/PR to main plus a weekly cron; NOT a required merge check |

Two non-obvious consequences:

- **`docker-publish` is a required merge check.** A Dockerfile or Docker-build breakage blocks ALL merges to main, even for changes that never touch Docker. If a PR is stuck on `docker-publish`, check the Docker build log before suspecting your change. Note the publish job lives inside `pytest.yml`, not a docker-named workflow file.
- **Every same-repo PR pushes a real `pr-N` image to GHCR.** Opening a PR has a publishing side effect.

How changes are classified and gated (what needs a PR, review depth, rollback discipline) is the libsync-change-control skill's territory — this section covers only what each check mechanically runs.

## When NOT to use this skill

- Deciding whether a change needs a PR, how it should be classified, or what the non-negotiable gates are and why → use **libsync-change-control**.
- Triaging a runtime failure, a confusing symptom, or a broken download → use **libsync-debugging-playbook**.
- Setting up the dev environment, `make deps`, submodule or Poetry problems → use **libsync-build-and-env**.
- Running, deploying, or releasing the app, or asking what data files are safe to delete → use **libsync-run-and-operate**.
- Needing measurement scripts or instrumentation beyond the test suite → use **libsync-diagnostics-and-tooling**.
- Turning an experimental hunch into an accepted change (methodology, not mechanics) → use **libsync-research-methodology**.

## Provenance and maintenance

Re-verify each fact before relying on it; all were last verified 2026-07-03 at v0.0.6:

- Test count and runtime: `time make test` (expect "154 passed" as of 2026-07-03).
- Per-file test counts: `poetry run pytest tests/ --collect-only -q | sed 's/::.*//' | sort | uniq -c | sort -rn`
- WebSocket coverage gap: `grep -rln -i 'websocket\|/ws' tests/` (no output = gap still open).
- No coverage tooling: `grep -in 'cov' pyproject.toml` (no pytest-cov = still true).
- Local ruff pin and lint failure: `poetry run ruff --version && poetry run ruff check backend/; echo "exit=$?"` (0.1.15 + 4 RUF100 + exit 1 = skew still present).
- Format skew: `poetry run ruff format --check backend/ tests/` (4 "would reformat" test files = skew still present).
- CI ruff passing on main despite local failure: `gh run list --workflow=ruff.yml --branch main --limit 3`
- Required merge checks: `gh api repos/arthursoares/libsync/branches/main/protection --jq '.required_status_checks.contexts'`
- Dead e2e job target: `ls tests/test_e2e_download.py` (missing = job still dead) and `grep -n 'test_e2e_download\|test_streamrip_versions_match' .github/workflows/pytest.yml`
- Frontend tests still unwired: `grep -n '"test"' frontend/package.json` (no match = still unwired) and `cd frontend && node --test 'tests/*.test.js'`
- Node glob requirement: `cd frontend && node --test tests/` (single failing "tests" entry = still required on your Node version).
- 80% threshold: `grep -n 'success_rate < 0.8' backend/services/download.py`
- Failed-status unreachability: `grep -n "not_downloaded" backend/services/download.py` and `grep -n "IN ('complete', 'failed')" backend/models/database.py`
- Pytest config: `sed -n '/\[tool.pytest.ini_options\]/,/^\[/p' pyproject.toml`
- RUF100 false-positive locations (line numbers drift): `poetry run ruff check backend/ | grep RUF100`
