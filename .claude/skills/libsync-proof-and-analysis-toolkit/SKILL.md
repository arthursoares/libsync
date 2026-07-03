---
name: libsync-proof-and-analysis-toolkit
description: Use when about to accept a root cause or "just try a fix" on libsync and want to PROVE it first — a Qobuz 401 that could be expired token vs app-id mismatch vs bad signing secret, a "library loads but downloads 401" split-brain, deciding if an SDK secret is valid without live guessing, a frontend event that never arrives, git history that looks wrong after the 2026-04-14 reroot, or SQLite state disagreeing with the UI.
---

# libsync Proof and Analysis Toolkit

First-principles analysis recipes for libsync (as of 2026-07-03, v0.0.6). The house rule this skill enforces: **prove it, don't just install it.** Before you accept a root cause or ship a fix, run an experiment whose result can only be explained by one mechanism. Every recipe below ends in a check you can run read-only, and each carries a worked example from this repo's real history.

Terms defined once:
- **X-App-Id** — HTTP header naming which Qobuz client app a request claims to be. Qobuz tokens are bound to the app that issued them.
- **Signing secret / app_secret** — the shared secret used to MD5-sign `track/getFileUrl` requests. Catalog and favorites reads do NOT need it; stream/download URLs do.
- **Dedup DB** — per-source SQLite file of already-downloaded track IDs (`data/downloads.db` for Qobuz, `data/downloads-tidal.db` for Tidal).
- **Reroot** — the 2026-04-14 history rewrite that cut 831 upstream commits off `main`'s visible ancestry.

---

## Recipe 1 — The discriminating 401 experiment

**Goal:** separate the three causes of a Qobuz 401 without guessing: expired/invalid token, X-App-Id mismatch, or bad signing secret.

**The asymmetry you exploit** (source: commit `6650993` message, and `sdks/qobuz_api_client/clients/python/qobuz` — `favorites.py` calls `favorite/getUserFavorites`, `catalog.py` calls `album/get`, `streaming.py` calls `track/getFileUrl`):

| Operation | Needs valid token | Needs matching X-App-Id | Needs signing secret |
|---|---|---|---|
| `favorites.get_albums` (`favorite/getUserFavorites`) | Yes | No (Qobuz ignores it here) | No |
| `catalog.get_album_with_tracks` (`album/get`) | Yes | Yes (validated) | No |
| `streaming.get_file_url` (`track/getFileUrl`) | Yes | Yes | Yes |

**Decision tree** (run these three probes in order, top to bottom):

1. Favorites read fails too → **token** problem (expired/invalid). Re-auth via the OAuth/PKCE flow in Settings.
2. Favorites read OK, album read (`album/get`) fails → **X-App-Id mismatch**. The stored `qobuz_app_id` does not match the app that issued `qobuz_token`.
3. Favorites AND album reads OK, only `get_file_url` / downloads fail → **signing secret** problem. The token/app pairing is fine; the app_secret is wrong or unresolved.

**Probes to drive the tree** (against your running instance — these hit the live Qobuz API, so they are out of scope for strictly read-only work; run them only when actively diagnosing):

```bash
# Probe A — favorites path (X-App-Id NOT validated here):
curl -s -X POST http://localhost:8080/api/library/refresh/qobuz | head -c 300
# Probe B — catalog path (album/* — X-App-Id validated):
curl -s "http://localhost:8080/api/library/search/qobuz?q=test" | head -c 300
# Probe C — which app identity is cached (read-only, no live API):
sqlite3 data/streamrip.db "SELECT key,value FROM config WHERE key IN ('qobuz_app_id','qobuz_app_secret');"
```

A fails → row 1 (token). A OK, B fails → row 2 (app-id mismatch); compare Probe C's `qobuz_app_id` against how the token was obtained (`304027809` OAuth vs `798273057` web-player). A and B OK, only downloads fail → row 3 (signing secret).

Caveat: the validated spec's status table (`docs/superpowers/specs/2026-04-08-qobuz-library-sdk-design.md`, ~line 396) says a bad app_id can surface as **400**, an invalid token as **401**. Real deployments in this repo's history saw **401** on the mismatch path (per `6650993`). Treat "which read tier fails" as the discriminator, not the numeric code alone.

**Worked example — the 2026-04-09 "library loads, downloads 401" saga.** On first boot `_init_clients` used a fallback app_id, but the real app_id was only learned later from the live bundle. aiohttp bakes request headers at session construction, so the session kept sending the wrong X-App-Id. Because Qobuz validates X-App-Id on `album/*` and the downloader but ignores it on `favorites/*`, `get_albums` succeeded while `get_album_with_tracks` and downloads returned 401 — exactly rows 1-vs-2/3 of the table above. The fix (`6650993`) closed and reopened the transport session after resolving the real app_id. Read the full arc: `git show 6650993 8e1f8e2 cfa4449 e4e0a1f`.

---

## Recipe 2 — Signature verification from first principles

**Goal:** prove whether a candidate app_secret is valid WITHOUT firing blind download attempts.

The formula is fixed in `sdks/qobuz_api_client/clients/python/qobuz/streaming.py` (`_compute_signature`). Re-read it before trusting this — the concatenation order is load-bearing and has no separators:

```
raw = "trackgetFileUrl" + "format_id" + format_id + "intent" + intent + "track_id" + track_id + timestamp + app_secret
request_sig = md5(raw).hexdigest()
```

`start_session` uses a DIFFERENT formula in the same file: `md5("qbz-1" + timestamp + app_secret)`. Do not cross them.

Recompute a signature yourself to confirm you understand the inputs before comparing against a captured request:

```bash
python3 -c 'import hashlib,time; ts=str(time.time()); secret="<candidate>"; print(hashlib.md5(f"trackgetFileUrlformat_id7intentstreamtrack_id19512574{ts}{secret}".encode()).hexdigest(), ts)'
```

**The safe validity test** is the SDK's own `find_working_secret` pattern (`qobuz/spoofer.py`, `find_working_secret(app_id, secrets, user_auth_token, test_track_id=19512574)`): it constructs a `QobuzClient` per candidate secret and calls `streaming.get_file_url(19512574, quality=2)` — a real signed request against a stable known track — returning the first secret that does not raise. This is the definitive proof a secret is valid: a good signature returns a file URL, a bad one is rejected. Note this DOES hit the live Qobuz API, so it is out of scope for read-only work — reach for it only inside the auth campaign (see sibling skill below), not for casual verification.

The two hardcoded constants to check first live in `qobuz/auth.py`: `APP_ID` (the OAuth app id, `"304027809"`) and `APP_SECRET` (a 32-hex-char MD5-style signing secret). Refer to the secret by name and file path — do not paste its value into skills, issues, or commit messages (external-positioning rule 2); read the current value with the Provenance grep below when you need it. `PRIVATE_KEY` in the same file is the OAuth code-exchange key, explicitly NOT the signing secret — do not substitute it.

---

## Recipe 3 — Traffic-capture-to-spec

**Goal:** turn observed API behavior into a validated contract, the method that produced `docs/superpowers/specs/2026-04-08-qobuz-library-sdk-design.md` (marked `Status: VALIDATED`).

Steps:
1. Capture real traffic (the spec used Proxyman → HAR: `Qobuz Helper_04-08-2026-10-09-15.har`, 142 API calls; a Chrome OAuth login flow HAR).
2. Derive each endpoint's contract from concrete request/response pairs — path, params, signing inputs, error shapes (the spec's status table came straight from an observed `{"status":"error","code":401,...}` body).
3. Encode the contract as SDK tests so drift is caught mechanically.

**Non-negotiable rule: captures contain live tokens and signing material — never commit them.** `.gitignore` already excludes `captures/` (verify: `grep -n captures .gitignore`, expect lines ~47–48). Confirm none slipped in: `git ls-files | grep -i captures` must return nothing. Redact tokens before pasting a capture anywhere. This is the same secret-handling discipline covered in the external-positioning sibling.

---

## Recipe 4 — Git archaeology under a rewritten history

**Goal:** read true ancestry in THIS clone, which hides upstream history behind a local git-replace ref. A fresh clone from GitHub has none of the objects below.

What exists locally (verify: `git for-each-ref | grep -E 'replace|original|pre-reroot'`):
- `refs/replace/eeee774… -> f5058e4` — makes `git log` present `f5058e4` as the root.
- `refs/heads/pre-reroot-backup-20260414-170428` — the pre-rewrite tip (`e8ad7aa`).
- `refs/original/refs/heads/feature/browse-library` — filter-branch leftover.

Commands:

| Question | Command | Expected (2026-07-03) |
|---|---|---|
| Is `main` disjoint from the backup? | `git merge-base main pre-reroot-backup-20260414-170428` | prints nothing, exit 1 (disjoint) |
| What is the reroot commit's TRUE parent? | `git --no-replace-objects cat-file -p eeee774` | `parent 5bcadd3…` |
| Name that parent | `git --no-replace-objects log -1 --format='%h %s' 5bcadd3` | `Fix deezer dynamic link parsing (#824)` |
| How many upstream commits are hidden? | `git --no-replace-objects log eeee774 --oneline \| wc -l` | 831 |

**Why bisect lies across the reroot boundary:** `git log` and `git bisect` silently follow the replace ref, so a range that straddles `f5058e4`/`eeee774` mixes rewritten and original objects and reports inconsistent counts. Compare `git log --oneline pre-reroot-backup-20260414-170428 | wc -l` (follows replace) against `git --no-replace-objects log --oneline pre-reroot-backup-20260414-170428 | wc -l` (true) — they differ by hundreds. Rule: **any bisect that must cross the reroot must run under `git --no-replace-objects`, or be split into two bisects that each stay on one side.**

The backup branch is local-only — `git ls-remote --heads origin | grep pre-reroot` returns nothing. If you need pre-reroot history, you must be in this clone; it cannot be re-fetched.

---

## Recipe 5 — SQLite forensics

**Goal:** interpret libsync's on-disk state. This skill teaches the METHOD; the runnable scripts live in the diagnostics sibling — do not duplicate them here.

Truths to check (all read-only `SELECT`; app DB is `data/streamrip.db`):

- **Schema version.** `sqlite3 data/streamrip.db "SELECT version FROM schema_version;"` must be `2` (`SCHEMA_VERSION=2` in `backend/models/database.py`). A `1` means the v2 migration (adds `bit_depth`/`sample_rate`/`local_folder_path`) has not run.
- **Boolean truth-tabling.** Config booleans are Pydantic-stringified as capitalized `True`/`False`. Probe: `sqlite3 data/streamrip.db "SELECT key,value FROM config WHERE lower(value) IN ('true','false');"`. Any value stored as lowercase `true` is a trap: `backend/api/library.py` (~lines 111, 161) reads `scan_sentinel_write_enabled` with a case-SENSITIVE `== "True"`, so a lowercase `true` silently disables sentinel writes. If you find one, that IS the bug.
- **Dedup-vs-albums consistency.** An album marked `complete` whose track IDs are absent from the dedup DB signals a bypassed mark path. Probe:
  ```bash
  sqlite3 data/streamrip.db "ATTACH 'data/downloads.db' AS dedup; SELECT count(*) FROM albums a WHERE a.source='qobuz' AND a.download_status='complete' AND EXISTS (SELECT 1 FROM tracks t WHERE t.album_id=a.id AND t.source_track_id NOT IN (SELECT id FROM dedup.downloads));"
  ```
  Expect `0`. Non-zero means an album was marked complete outside the `mark_album_downloaded` primitive (`backend/services/scan.py`) — e.g. the download service or legacy `/api/downloads/scan` path, which write `download_status` without populating dedup.
- **WAL sidecar semantics.** `data/streamrip.db-wal` and `-shm` are the write-ahead log (`PRAGMA journal_mode=WAL`, `database.py`). They are live state, not junk — a non-empty `-wal` means uncommitted-to-main-file writes. Never delete them under a running backend; they are safe to leave and are checkpointed automatically.
- **Legacy files.** `data/downloads_albums.db` and `data/failed.db` are referenced by NO backend code (`grep -rn 'downloads_albums\|failed.db' backend` returns nothing) — leftover streamrip-CLI artifacts, safe to ignore.

---

## Recipe 6 — Event-flow tracing (prove wiring, in either direction)

**Goal:** prove whether a backend event can ever reach the frontend, before you debug "the UI isn't updating."

Walk three sites in order:
1. **Publish site** — `grep -rn 'event_bus.publish\|\.publish(' backend --include='*.py'`.
2. **Bridge list** — `backend/main.py` (`create_app`, ~lines 252–266) subscribes exactly these 7 types to the WebSocket: `download_progress`, `download_complete`, `download_failed`, `sync_started`, `sync_complete`, `library_updated`, `token_expired`. The `EventBus` (`backend/services/event_bus.py`) silently drops any event with no subscriber.
3. **Frontend registration** — `grep -rn "onEvent(" frontend/src/lib/stores`.

An event reaches the UI only if it is published AND in the bridge list AND has an `onEvent` handler. A gap at any step is dead wiring:

| Event | Published? | In bridge list? | Frontend listens? | Verdict |
|---|---|---|---|---|
| `token_expired` | No (grep `backend` finds only `main.py`) | Yes | No | Bridged but never fired — dead sender |
| `album_status_changed` | Yes (`api/library.py` ~125, ~143) | **No** | Yes (`stores/library.ts` ~29) | Published + listener, but never bridged — listener can never fire |

**Worked example:** "marking an album downloaded doesn't update the library view live." Tracing shows `album_status_changed` is published and the frontend registers `onEvent('album_status_changed', …)`, so both ends look correct — but it is absent from `main.py`'s bridge list, so `EventBus` drops it. The method exposes dead wiring the endpoint-level view hides. (The UI updates via other paths / polling; scan progress is polled via `GET /api/library/scan-fuzzy/{job_id}` by design.)

---

## Recipe 7 — Mechanism-explains-all-observations discipline

**Goal:** reject a root cause that explains only the positive observations. Before accepting one, list EVERY observation — including the negatives and the things that kept working — and confirm the proposed mechanism predicts each.

**Worked example — "headers baked at session construction."** During the 2026-04-09 saga the candidate root cause was "wrong X-App-Id." Truth-table it against all observations:

| Observation (incl. negatives) | Does "aiohttp bakes headers at session construction, and Qobuz validates X-App-Id only on album/download endpoints" predict it? |
|---|---|
| Favorites/library loaded fine | Yes — Qobuz ignores X-App-Id on `favorites/*` |
| Album reads and downloads 401'd | Yes — validated there, header was wrong |
| Re-login did NOT fix it | Yes — a new token still flows through the already-constructed session with the stale baked header |
| Forcing OAuth app_id (`8e1f8e2`) fixed some users but BROKE a different cohort | Yes — web-player-token users (app `798273057`) then had a mismatched header; the mechanism predicts their downloads break (fixed by `cfa4449`, "never overwrite cached app_id") |

Every row, including the two negatives, is predicted — that is what promotes it from hunch to root cause. A weaker theory ("token expired") fails rows 1 and 3 immediately. Do this table before writing the fix.

---

## When NOT to use this skill

- Want a fast symptom → cause lookup, not a proof method? Use **libsync-debugging-playbook**.
- Want to RUN forensic scripts rather than learn the interpretation? Use **libsync-diagnostics-and-tooling** (it ships the runnable scripts this skill references).
- Working the Qobuz auth problem end-to-end with live API calls and decision gates? Use **libsync-qobuz-auth-campaign** — it is the executable runbook; this skill only teaches the discriminating experiments feeding it.
- Want the narrative of what was already tried and settled? Use **libsync-failure-archaeology**.
- Need codec / OAuth-flow / app-id-model theory? Use **qobuz-tidal-domain-reference**.
- Need the invariants a fix must not break? Use **libsync-architecture-contract**.

Change-control non-negotiables (PRs + required CI checks `backend-tests`, `frontend-build`, `docker-publish`, `ruff`) still apply to anything you ship after proving a cause — see **libsync-change-control**. This skill helps you build evidence; it never authorizes routing around review.

---

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it:

- Signature formula and quality map: `sed -n '15,30p' sdks/qobuz_api_client/clients/python/qobuz/streaming.py`
- `find_working_secret` / test track ID: `grep -n 'def find_working_secret\|test_track_id' sdks/qobuz_api_client/clients/python/qobuz/spoofer.py`
- Qobuz hardcoded constants: `grep -n 'APP_ID\|APP_SECRET\|PRIVATE_KEY' sdks/qobuz_api_client/clients/python/qobuz/auth.py`
- Which endpoints each SDK area calls: `grep -rn 'favorite/getUserFavorites\|album/get\|track/getFileUrl' sdks/qobuz_api_client/clients/python/qobuz`
- 401-saga commit arc: `git show -s 6650993 8e1f8e2 cfa4449 e4e0a1f`
- Reroot refs and true parent: `git for-each-ref | grep -E 'replace|original|pre-reroot'` and `git --no-replace-objects cat-file -p eeee774`
- Hidden upstream commit count: `git --no-replace-objects log eeee774 --oneline | wc -l` (expect 831)
- Backup branch is local-only: `git ls-remote --heads origin | grep pre-reroot` (expect empty)
- WS bridge list (7 types): `sed -n '252,266p' backend/main.py`
- Event dead-wiring: `grep -rn 'album_status_changed\|token_expired' backend --include='*.py'` and `grep -rn 'onEvent(' frontend/src/lib/stores`
- Schema version: `sqlite3 data/streamrip.db "SELECT version FROM schema_version;"` (expect 2)
- Case-sensitive bool trap: `grep -n 'scan_sentinel_write_enabled' backend/api/library.py`
- Captures ignored, none committed: `grep -n captures .gitignore` and `git ls-files | grep -i captures`
- Submodule pin (as of 2026-07-03): `git submodule status` (expect `5e66211…`)
- Validated spec status/date: `sed -n '1,5p' docs/superpowers/specs/2026-04-08-qobuz-library-sdk-design.md`
