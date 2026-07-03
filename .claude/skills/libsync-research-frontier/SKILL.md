---
name: libsync-research-frontier
description: "Use when asked what libsync could do beyond existing tools, when brainstorming roadmap or research directions, or when someone proposes: making the scan matcher learn from user confirmations, formalizing the Qobuz/Tidal SDK facade into a contract or adding a third source, detecting upstream secret/app-id rotation before it breaks downloads, or running libsync offline/air-gapped (fonts, cover art). Also use to check whether an ambitious idea already has a vetted framing before starting work."
---

# libsync Research Frontier

This skill catalogs open problems where libsync is positioned to advance the state of the art. Every framing below is a **CANDIDATE — maintainer-unconfirmed**. Nothing here is committed roadmap; the maintainer has not signed off on any of it. Treat each entry as a well-grounded hypothesis, not a plan.

Rules of engagement before pursuing any candidate:

1. Read `libsync-research-methodology` first. It defines the discipline for turning a hunch into an accepted change. This skill only says *what* is open; that skill says *how* to pursue it.
2. All changes go through normal change control: a PR on a branch, passing the required CI checks `backend-tests`, `frontend-build`, `docker-publish` (all in `.github/workflows/pytest.yml`) and `ruff` (`.github/workflows/ruff.yml`). Never route around this. Note that local `make lint` fails on clean main due to a local-vs-CI ruff version skew (as of 2026-07-03) — do not run `--fix` blindly; see `libsync-change-control`.
3. Run `make test` before and after any experiment. As of 2026-07-03 (v0.0.6) it is 154 tests passing in ~2–8 seconds with no credentials needed.
4. If a candidate's premise fails verification (each entry lists a falsifier), record the negative result — `libsync-failure-archaeology` is the pattern for that — and stop.

## Candidate 1: Learning library matcher

**Status: CANDIDATE — maintainer-unconfirmed.** The library-scan plan itself names this as deferred work: the "Open follow-ups (out of scope)" section at the end of `docs/superpowers/plans/2026-04-18-library-scan-fuzzy-match-plan.md` lists "Learning from user-confirmed ambiguous matches (rule tuning stays manual)" (verified 2026-07-03).

| Aspect | Detail |
|---|---|
| Why current SOTA falls short | Tools in the beets class match local files to canonical releases with static fuzzy rules and hand-tuned thresholds. They do not learn from the corrections users make in review UIs, so the same ambiguity is re-litigated forever. |
| libsync's specific asset | The full loop already exists in code: a deterministic matcher (`backend/services/scan.py`: `normalize()`, `build_library_index()`, `classify()` — exact match on normalized `(artist, album)` keys plus a bit-depth compatibility filter, auto-match only on a singleton candidate with a reliable artist), a review UI (`frontend/src/lib/components/ScanReview.svelte`, whose `confirm()` calls through to `api.library.markDownloaded`), and a single choke point for confirmations (`POST /api/library/albums/{album_id}/mark-downloaded` in `backend/api/library.py` → `mark_album_downloaded` in `backend/services/scan.py`). Every user-confirmed match already flows through one function. |
| The gap | Decisions are not persisted as decisions. The app DB schema (`backend/models/database.py`) has only `albums`, `tracks`, `sync_runs`, `config`, `schema_version` — no table records which candidate was confirmed for which folder, with what features, and rejections/dismissals are not recorded at all. |

First three steps in this repo:

1. Add a decision log: a new table (schema migration in `backend/models/database.py`) written from `mark_album_downloaded` and from a new explicit-reject path, storing the folder's `FolderMeta` features (normalized artist/album, bit depth, track count), the candidate set, and the user's choice. This is additive and does not change matching behavior.
2. Build a labeled evaluation set and measure the current matcher's precision/recall on it. `tests/test_scan_matcher.py` and `tests/test_scan_normalize.py` already show the harness pattern (pure functions, no I/O); extend that into a fixture corpus of real-world folder names → correct album IDs.
3. Prototype threshold/rule tuning from the stored decisions offline (a script, not a code path), and compare against the step-2 baseline on a held-out split.

**You have a result when:** the auto-match rate on a held-out labeled set improves by a measured percentage with zero new false positives, numbers produced by the step-2 harness — not judged by eye.

**Cost/risk:** a schema migration (v2 → v3; existing deployments must migrate cleanly — see the v1→v2 backfill precedent in `backend/models/database.py`), and the risk that a learned matcher silently auto-confirms wrong albums, which then writes dedup-DB rows and `.streamrip.json` sentinels via `mark_album_downloaded`. Keep learning offline/advisory until the zero-false-positive milestone is met.

**Falsifier:** if the step-2 measurement shows the current exact-normalized-key matcher already achieves near-ceiling precision/recall on realistic corpora, learning adds complexity for no measurable gain — drop the candidate.

## Candidate 2: Source-agnostic SDK contract

**Status: CANDIDATE — maintainer-unconfirmed.**

| Aspect | Detail |
|---|---|
| Why current SOTA falls short | Multi-service music tooling typically hardcodes per-service clients; "add a new service" means touching every call site. There is no widely adopted formal contract for streaming-service SDKs. |
| libsync's specific asset | Two real, independently implemented SDKs already conform to one implicit facade: `client.catalog` / `client.favorites` / `client.streaming` (Qobuz `clients/python/qobuz/client.py`; Tidal `clients/python/tidal/tidal/client.py` inside the `sdks/qobuz_api_client` submodule), plus an identical `AlbumDownloader` callback protocol — `on_track_start(num, title)`, `on_track_progress(num, bytes_done, bytes_total)`, `on_track_complete(num, title, success)` — in both downloaders. The backend consumes the contract source-agnostically via `hasattr(client, "catalog")` checks (`backend/services/library.py`, `backend/services/download.py`). |
| The gap | The contract is implicit and already leaking divergences the backend must guard by hand: `TrackResult.path` (Qobuz) vs `TrackResult.file_path` (Tidal); `DownloadConfig` accepts `download_booklets` only for Qobuz (the backend adds that kwarg only when `source == "qobuz"` in `backend/services/download.py`, `_build_download_config` area) and `write_metadata_file` only for Tidal; Qobuz has `playlists` and `discovery` facades that Tidal lacks; source names are hardcoded as `("qobuz", "tidal")` tuples in `backend/main.py` (auto-sync source pick) and `backend/api/auth.py`. `tests/test_library_sdk.py` re-implements the contract as hand-rolled mocks (`MockAlbum`, `MockFavoriteAlbums`, ...), which is itself evidence the contract exists but is undocumented. |

First three steps in this repo (and its submodule):

1. Extract the implicit contract into a documented protocol: enumerate, from `backend/services/library.py`, `backend/services/download.py`, and `backend/services/sync.py`, exactly which attributes and signatures the backend actually calls. This is a read-only inventory; produce it as a doc first.
2. Enumerate the known divergences (start with the `TrackResult` field name, the `DownloadConfig` kwarg sets, and the facade-breadth difference) and classify each as contract-mandatory vs. optional-capability.
3. Write contract tests. The tests belong in the SDK repo (`arthursoares/qobuz_tidal_api_client`, consumed here as the `sdks/qobuz_api_client` submodule) — SDK changes follow that repo's process, then a submodule pin bump PR here. In this repo, replace the ad-hoc mocks in `tests/test_library_sdk.py` with a single mock source that conforms to the documented protocol.

**You have a result when:** a third source — a mock source is sufficient — passes the contract test suite and drives the backend's library/download paths without backend code changes beyond source registration.

**Cost/risk:** cross-repo coordination (SDK repo + submodule pin bumps), and the risk of over-formalizing: with only two implementations, any "contract" may just codify their accidental commonalities. Note also that the submodule working tree currently carries uncommitted lint-only edits across ~31 files (as of 2026-07-03) — reconcile that before basing SDK work on the local tree; see `libsync-build-and-env`.

**Falsifier:** if step 1's inventory shows the backend's real dependency surface is small and stable (a handful of methods), a full protocol + contract suite may cost more than the `hasattr` status quo — the honest outcome is "document the surface, skip the formalism".

## Candidate 3: Resilient reverse-engineered auth

**Status: CANDIDATE — maintainer-unconfirmed.** The *execution* of the hardest live version of this problem is owned by the sibling skill `libsync-qobuz-auth-campaign` — read it before doing anything here. This entry only frames the research angle: making reverse-engineered auth *observable and testable* rather than "breaks silently when upstream rotates".

| Aspect | Detail |
|---|---|
| Why current SOTA falls short | The ecosystem norm for reverse-engineered streaming clients is silent breakage: baked-in app IDs and secrets work until the vendor rotates them, then every deployment fails with opaque 401s and no early warning. |
| libsync's specific asset | A three-tier secret-resolution chain already exists in `backend/main.py` (`_resolve_qobuz_credentials`): (1) user override via the `qobuz_app_secret` config key, (2) the hardcoded `qobuz.auth.APP_SECRET` when the token's app ID matches the OAuth app, (3) a spoofer fallback (`qobuz.spoofer.fetch_app_credentials` + `find_working_secret`, which live-verifies each candidate secret against a real `get_file_url` call). The verification machinery — actually testing a secret before trusting it — is the rare part; most tools just hope. |
| The gap | The resolution outcome is invisible: it is logged at INFO/WARNING via `logging.getLogger("streamrip")`, but the backend configures no logging handler (no `basicConfig`/`dictConfig` anywhere in `backend/`, verified 2026-07-03), so at runtime the operator sees nothing. The `token_expired` WebSocket event type is registered in `backend/main.py` but nothing publishes or consumes it — dead stub wiring. When verification fails, the code falls back to the first unverified candidate with only an invisible warning. |

First three steps in this repo:

1. Make the resolution outcome observable: surface which tier resolved the secret (and whether live verification passed) via an API field the Settings page can render — not just a log line.
2. Add an automated canary check: a unit-testable path that simulates upstream rotation (patch the resolved secret to an invalid value, as `tests/test_auto_sync_hotreload.py` already patches `backend.main._resolve_qobuz_credentials`) and asserts the failure is detected and surfaced, not swallowed.
3. Write the rotation runbook: a documented, ordered recovery procedure (user-override key → re-OAuth → spoofer) so an operator hit by a real rotation has steps instead of archaeology.

**You have a result when:** a simulated secret rotation is detected and surfaced to the operator within one operation (the first failing download or auth check), verified by an automated test — no live-API calls needed for the test.

**Cost/risk:** touching auth paths risks regressing working deployments; anything that calls the live spoofer or Qobuz endpoints must never run in CI or tests. Secret values must never appear in logs or UI — see `libsync-external-positioning` for secret-handling discipline.

**Falsifier:** if instrumentation shows tier-2 (the hardcoded `APP_SECRET`) serves essentially all real deployments and rotations are rare enough that the manual override suffices, the canary machinery is over-engineering — the runbook alone is the right deliverable.

## Candidate 4: Fully self-contained deployment

**Status: CANDIDATE — maintainer-unconfirmed.** The project positions itself as self-hosted, but two external couplings remain (both verified 2026-07-03, v0.0.6).

| Aspect | Detail |
|---|---|
| Why current SOTA falls short | Most self-hosted media web UIs quietly depend on third-party CDNs (fonts, artwork), so "self-hosted" deployments leak requests to Google and vendor image hosts, and air-gapped or privacy-strict networks get degraded UIs. |
| libsync's specific asset | The app is otherwise a single container serving its own static SPA from `backend/static/` — the remaining external surface is small and enumerable. |
| The gaps | (a) The Atkinson Hyperlegible Next font is loaded from `fonts.googleapis.com` in `frontend/src/app.html` (lines 6–7 as of 2026-07-03); offline it silently falls back to sans-serif, breaking the design system's single-font rule. (b) Cover art is hotlinked: sync stores the streaming service's CDN image URL (`backend/services/library.py`, `cover_url` from `image.get("large") or image.get("small")`) and the frontend renders `<img src={album.cover_url}>` (`frontend/src/lib/components/AlbumCard.svelte`); there is no proxy or cache in the backend. |

First three steps in this repo:

1. Bundle the font: self-host the woff2 files as static assets and replace the Google Fonts `<link>` in `frontend/src/app.html`. Check the font's license terms permit redistribution before bundling (Atkinson Hyperlegible is commonly OFL-licensed, but verify the "Next" variant's license — unverified as of 2026-07-03).
2. Add a cover-art proxy/cache endpoint in the backend that fetches the stored CDN URL once and serves it from a cache directory under `data/` (the established runtime-artifact location — see `libsync-run-and-operate` for the data-artifact map), then point `cover_url` consumers at it.
3. Write an offline smoke test: build and run the Docker image with outbound network blocked and script assertions that the library page serves covers and the correct font from the container itself.

**You have a result when:** a fresh deploy on a network with no outbound internet access renders library cover art and correct typography, demonstrated by the step-3 smoke test.

**Cost/risk:** cache growth in `data/` (needs an eviction or size-cap decision), the proxy adds a fetch path that must respect the same path-traversal discipline as the existing static file serving, and caching vendor artwork server-side may have licensing implications distinct from hotlinking — flag to the maintainer before shipping (inferred — confirm with maintainer). The image repo `ghcr.io/arthursoares/libsync` pull itself still requires network at install time; "self-contained" here means *runtime*, not install.

**Falsifier:** if the maintainer's target deployments always have outbound internet (a plausible reading, since downloads themselves require live Qobuz/Tidal access), the air-gapped framing collapses to a privacy nicety — the font bundling may still be worth it standalone, the art cache may not.

## When NOT to use this skill

- Executing the Qobuz auth resilience work — use `libsync-qobuz-auth-campaign` (the decision-gated campaign); Candidate 3 here is only the research framing.
- The discipline for pursuing any candidate (hypotheses, evidence standards, when to stop) — use `libsync-research-methodology`.
- Measuring anything (precision/recall harnesses, instrumentation) — recipes live in `libsync-diagnostics-and-tooling` and `libsync-proof-and-analysis-toolkit`.
- How a change gets classified, gated, and reviewed — use `libsync-change-control`.
- Understanding existing invariants you must not break while experimenting — use `libsync-architecture-contract`.
- Codec/OAuth/signing background needed to reason about Candidates 2–3 — use `qobuz-tidal-domain-reference`.
- What already failed and why (avoid re-running dead ends) — use `libsync-failure-archaeology`.

## Provenance and maintenance

Every fact above was verified against the repo on 2026-07-03 at v0.0.6. Re-verify before relying on any of it. When grepping repo-wide, exclude `.claude`, `node_modules`, `.svelte-kit`, and `sdks` unless intended — `.claude/worktrees/` contains full copies of deleted CLI-era code.

- Plan still lists learning as open follow-up: `tail -20 docs/superpowers/plans/2026-04-18-library-scan-fuzzy-match-plan.md`
- Matcher shape (exact normalized keys, singleton auto-match): `grep -n "def classify\|def normalize\|by_full_key" backend/services/scan.py`
- Confirm flow reaches mark_album_downloaded: `grep -n "mark-downloaded" backend/api/library.py && grep -n "markDownloaded" frontend/src/routes/settings/+page.svelte`
- No decision-log table yet: `grep -n "CREATE TABLE" backend/models/database.py`
- Facade shape in both SDKs: `grep -n "self.catalog\|self.favorites\|self.streaming\|self.playlists\|self.discovery" sdks/qobuz_api_client/clients/python/qobuz/client.py sdks/qobuz_api_client/clients/python/tidal/tidal/client.py`
- TrackResult field divergence: `grep -n -A7 "class TrackResult" sdks/qobuz_api_client/clients/python/qobuz/downloader.py sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py`
- DownloadConfig kwarg guard: `grep -n "download_booklets" backend/services/download.py`
- Hardcoded source tuples: `grep -rn '("qobuz", "tidal")' backend/`
- Three-tier secret resolution: `grep -n "_resolve_qobuz_credentials\|qobuz_app_secret\|APP_SECRET\|find_working_secret" backend/main.py`
- Spoofer verification: `grep -n "def find_working_secret\|def fetch_app_credentials" sdks/qobuz_api_client/clients/python/qobuz/spoofer.py`
- No logging config (INFO invisible at runtime): `grep -rn "basicConfig\|dictConfig" backend/` (expect no hits)
- token_expired is still a dead stub: `grep -rn "token_expired" backend/ frontend/src/` (expect only the registration in backend/main.py)
- Google Fonts coupling: `grep -n "fonts.googleapis" frontend/src/app.html`
- Cover art hotlink: `grep -n "cover_url" backend/services/library.py frontend/src/lib/components/AlbumCard.svelte`
- Required CI check names: `grep -n "^  [a-z-]*:" .github/workflows/pytest.yml .github/workflows/ruff.yml`
- Test count/suite health: `make test` (154 passed as of 2026-07-03)
