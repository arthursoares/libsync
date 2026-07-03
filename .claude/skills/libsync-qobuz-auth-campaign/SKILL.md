---
name: libsync-qobuz-auth-campaign
description: Use when building in-app Qobuz token-expiry recovery, wiring the dead `token_expired` WebSocket event to a real publisher and Settings banner, making Qobuz download-secret resolution observable, or hardening the play.qobuz.com spoofer chain. Symptoms — every Qobuz call 401s after a period of working; Qobuz 401 shows only a generic "Download failed" toast with no re-auth prompt; users must restart or re-login to recover an expired Qobuz token; secret-tier winner is invisible in logs.
---

# Libsync Qobuz Auth Resilience Campaign

## What this is

An executable, decision-gated campaign to close libsync's hardest live gap:
**a Qobuz token can expire or a signing secret can rotate, and the app has no
in-app recovery path.** You work through phases; each phase ends with a **gate
table** (`command | expected | if-not branch`). Do not advance past a red gate.

Jargon defined once:
- **Token expiry** — the Qobuz `user_auth_token` becomes invalid; every Qobuz
  API call then returns HTTP 401. Qobuz has **no refresh endpoint**, so recovery
  means re-authenticating, not silently refreshing (contrast Tidal, which the
  SDK auto-refreshes).
- **App-id / app-secret** — Qobuz binds each token to the app that issued it
  (`X-App-Id` header). Downloads additionally need an **app-secret** to sign the
  `track/getFileUrl` request (an MD5 signature). See `qobuz-tidal-domain-reference`
  for the codec/OAuth/signing theory — read it before Phase 5.
- **Spoofer** — `qobuz.spoofer` scrapes `play.qobuz.com`'s JS bundle for a
  working app-secret when no hardcoded one applies (web-player tokens).
- **EventBus / WS bridge** — the in-process pub/sub (`backend/services/event_bus.py`);
  only 7 named event types are re-broadcast to the browser WebSocket in
  `backend/main.py` (`create_app`). Events with no subscriber are dropped silently.

### Campaign target is inferred, not maintainer-blessed

This being "the hardest problem worth a campaign" is an **inference — confirm with
the maintainer before major investment.** CLAUDE.md lists "Qobuz token refresh is
manual (no OAuth refresh endpoint)" under Known Issues (as of 2026-07-03, v0.0.6),
which supports the direction, but no spec or plan commits to building this. Phases
1–4 are low-risk and reversible; Phase 5 touches signing internals and needs an
explicit go/no-go.

## (0) Problem statement and definition of done

Today, when Qobuz returns 401 mid-operation:
- **Download path** (`backend/services/download.py`, `_process_queue`): the SDK
  raises `qobuz.errors.AuthenticationError` (a 401 subclass — verified in
  `sdks/qobuz_api_client/clients/python/qobuz/errors.py`). The service catches
  the generic `Exception`, marks the item `failed`, reverts the album to
  `not_downloaded`, and publishes **`download_failed`** with `str(e)`. The user
  sees a red "Download failed" toast (`frontend/src/routes/+layout.svelte`) and
  no hint that the fix is re-auth.
- **Library/sync paths**: the 401 propagates as a 500 or an empty result; no
  event fires.
- **`token_expired` is dead wiring.** It is *subscribed* to the WS bridge in
  `backend/main.py` (`create_app`) but **nothing anywhere publishes it** — grep
  across `backend/` and the SDK returns only the subscription site (verified
  2026-07-03). The frontend has **no listener** for it either.

**Definition of done (all must hold, all machine-checked — never judged by eye):**

| # | Criterion | How it is verified |
|---|-----------|--------------------|
| D1 | A Qobuz 401 in any operation publishes exactly one `token_expired` EventBus event per failure burst (not one per track). | New backend test asserting event count. |
| D2 | The browser receives `token_expired` over `/api/ws` and shows a persistent banner routing to Settings. | Manual observation protocol (Phase 3 gate). |
| D3 | Re-authenticating from Settings clears the banner and restores downloads. | Manual gate (Phase 4). |
| D4 | Which secret tier resolved (override / hardcoded / spoofer) is observable from a diagnostic, not inferred. | Phase 5 diagnostic output. |
| D5 | All four CI checks stay green: `backend-tests`, `frontend-build`, `docker-publish`, `ruff`. | CI on the PR. |

## Ground rules (fenced-off wrong paths)

These are settled by incident history — do not relitigate. Cross-reference
`libsync-failure-archaeology` for the full chronicle.

| Never do this | Why / incident |
|---------------|----------------|
| Unconditionally overwrite the cached `qobuz_app_id`. | `8e1f8e2` forced `OAUTH_APP_ID` for everyone and broke web-player-token users; `cfa4449` reverted it. Rule since: only cache when unset, never clobber. |
| Force `OAUTH_APP_ID` (304027809) for all users. | Same incident. OAuth tokens and web-player tokens (`798273057`) are bound to *different* apps; `X-App-Id` must match the issuing app. |
| Auto-trigger `_reload_clients` on a detected 401. | `_reload_clients` (`backend/api/config.py`) `__aexit__`s the live SDK sessions; doing so mid-download kills the in-flight transfer (headers are baked at aiohttp session construction — that is *why* `_reload_clients` exists, but it is a user-initiated action, not an automatic one). Detection must be observe-only. |
| Rebuild SDK clients per-request to "get fresh headers". | Same header-baking reason; per-request rebuild is wasteful and racy. |
| Call Qobuz HTTP directly from the backend, bypassing the SDK. | All Qobuz access goes through the SDK submodule facade (`client.catalog` / `client.favorites` / `client.streaming`). Bypassing it duplicates signing/auth logic. |
| Add a periodic "is the token still valid?" probe. | Rejected by default: it burns the Qobuz rate limit (SDK default 30 req/min) and risks account flags. Detect on real 401s instead. |
| Hammer live Qobuz endpoints in a test loop. | Ban risk. All tests here inject fake downloaders/transports — zero network. |

## (1) Phase 1 — Reproduce and instrument (no live-API abuse)

**Goal:** make a Qobuz 401 happen deterministically and record exactly what the
system does today, without touching the network.

### Preferred method: a backend test that injects a 401-raising downloader

`tests/test_download_failure_threshold.py` is your template — it already injects
a fake `qobuz.AlbumDownloader` via `patch("qobuz.AlbumDownloader", ...)` and
drives `DownloadService._process_queue()` with an in-memory DB and `EventBus`.
Copy its fixtures. Make the fake downloader's `download()` raise the real SDK
error:

```python
from qobuz.errors import AuthenticationError

def _raising_downloader(client, config, *, on_track_start=None,
                        on_track_progress=None, on_track_complete=None):
    inst = MagicMock()
    inst.download = AsyncMock(side_effect=AuthenticationError(401, "Invalid token"))
    return inst
```

Subscribe a recording handler to the bus and assert what fires:

```python
seen = []
event_bus.subscribe("download_failed", lambda d: seen.append(("download_failed", d)))
event_bus.subscribe("token_expired", lambda d: seen.append(("token_expired", d)))
with patch("qobuz.AlbumDownloader", new=_raising_downloader):
    await service._process_queue()
# Baseline (today): download_failed fires, token_expired does NOT.
```

### Secondary method: manual, against a local dev server

Only if you must see the UI. **Caveat:** `PATCH /api/config` with a *changed*
`qobuz_token` triggers `_reload_clients`, which runs `_resolve_qobuz_credentials`
— and if the client is a web-player app that path calls the spoofer, which
**hits `play.qobuz.com` (live network)**. To avoid that, do not change the token
via the API to simulate expiry; instead corrupt the stored token directly in the
DB while the server is stopped, then start the server so the bad token is only
exercised on the next real operation:

```bash
# server stopped; back up first
sqlite3 data/streamrip.db "UPDATE config SET value='EXPIRED_INVALID' WHERE key='qobuz_token';"
make dev-backend    # then trigger a download from the UI and watch the browser + terminal
```

Restore afterward from your backup (the real token also lives in `data/streamrip.db`).

### Phase 1 gate

| Command | Expected | If not → branch |
|---------|----------|------------------|
| Run your new injecting test. | `download_failed` recorded; `token_expired` absent. | If `token_expired` already fires, someone finished detection — jump to Phase 3. |
| Observe the failing operation. | Only **downloads** 401 while **library loads fine**. | If library ALSO 401s, the token is genuinely dead (expiry) → this campaign. If **only downloads** 401 while library works, you are in **app-id / signing-mismatch territory, not expiry** — stop, use the discriminating experiment in `libsync-proof-and-analysis-toolkit` and the app-id rules in `qobuz-tidal-domain-reference`. |
| `grep -rn token_expired backend/ frontend/src/` | Subscription in `backend/main.py` only; no publisher, no listener. | If a publisher exists, update this skill's baseline. |

## (2) Phase 2 — Detection (publish `token_expired`)

**Write the test first** (TDD — see `superpowers:test-driven-development`). The
test from Phase 1 becomes the spec: after a Qobuz `AuthenticationError`, the bus
must carry **one** `token_expired` per failure burst.

### Solution menu (ranked)

| Option | What | Obligations | Verdict |
|--------|------|-------------|---------|
| **(a) Backend service-layer catch** | In the download/library/sync services, catch `qobuz.errors.AuthenticationError` specifically and `event_bus.publish("token_expired", {"source": "qobuz"})` before/alongside the existing failure handling. | Must **dedup/throttle**: one event per burst, not per track. Keep publishing `download_failed` too (don't break existing toasts). | **Preferred** — the WS bridge already forwards `token_expired`; this is the designed-but-unfinished path. |
| (b) SDK-level callback hook | Add an `on_auth_error` callback to the SDK `AlbumDownloader` / transport. | Requires an SDK change + submodule pin bump + `make deps`; heavier, cross-repo. | Defer unless (a) proves insufficient. |
| (c) Periodic validity probe | Background task pings Qobuz. | Burns rate limit; ban risk. | **Rejected by default** (see Ground rules). |

### Insertion points (option a)

- **Download:** `backend/services/download.py`, the `except Exception as e:` block
  in `_process_queue` (the block that sets `item["status"] = "failed"` and
  publishes `download_failed`). Add an `isinstance(e, AuthenticationError)` branch
  that also publishes `token_expired`. Import lazily (`from qobuz.errors import
  AuthenticationError`) to keep Tidal-only failures unaffected.
- **Library / sync:** wrap the SDK calls in `backend/services/library.py` and
  `backend/services/sync.py` similarly. These are separate bursts; a per-source
  throttle (e.g. suppress duplicate `token_expired` within N seconds via a
  timestamp on the service) satisfies the "one per burst" obligation.

Publish through the existing `EventBus` — it is already bridged to the WebSocket
for `token_expired` in `backend/main.py` (`create_app`), so **no `main.py` change
is needed for the event to reach the browser** (verify the name matches exactly).

### Phase 2 gate

| Command | Expected | If not → branch |
|---------|----------|------------------|
| `make test` (or your targeted test). | New test passes: one `token_expired` per burst; `download_failed` still fires. | If N `token_expired` (one per track), your throttle is missing — fix dedup. |
| `grep -n AuthenticationError backend/services/*.py` | Caught in the service layer, imported lazily. | If caught too broadly (all `Exception` → `token_expired`), Tidal/network errors would false-positive — narrow it. |
| `make test` full suite. | 154 passing before your change stays green plus your additions (154 as of 2026-07-03, v0.0.6). | Any regression → revert and narrow. |

## (3) Phase 3 — Surface (frontend banner)

The frontend already has the listener pattern: `onEvent(type, handler)` in
`frontend/src/lib/stores/websocket.ts`, used in `+layout.svelte` and the stores.

**Cautionary example — do not repeat it:** `frontend/src/lib/stores/library.ts`
registers `onEvent('album_status_changed', ...)` but the backend **never bridges
that event**, so the listener can never fire (verified 2026-07-03). Shipping a
listener without confirming the backend publishes AND bridges the event is a
known trap. For `token_expired` the bridge already exists (Phase 2 relies on it),
so confirm end-to-end delivery before declaring done.

### Work

- Add `onEvent('token_expired', ...)` in `+layout.svelte` (alongside the existing
  `download_failed` handler) or a dedicated store.
- Render a **persistent banner** (not a transient toast — expiry needs a durable
  call to action) that deep-links to `/settings`. The layout already imports
  `goto` from `$app/navigation` and has `/settings` nav.
- Follow the design system (`frontend/src/lib/design-system/tokens.css`):
  `border-radius: 0`, solid 0px-blur shadows, Atkinson Hyperlegible, dark-mode
  default, 80ms hover transition only.

### Phase 3 gate — manual observation protocol

| Step | Expected |
|------|----------|
| `make dev`, corrupt the token per Phase 1 secondary method, trigger a Qobuz download. | Browser DevTools → Network → WS `/api/ws` frame shows `{"type":"token_expired",...}`. |
| Observe the page. | Persistent banner appears; clicking it navigates to `/settings`. |
| `make test` + `cd frontend && npm run build` (matches CI `frontend-build`). | Both green. |

If the WS frame never arrives, the event name in Phase 2 does not match the
bridged name in `backend/main.py` — reconcile the exact string.

## (4) Phase 4 — Recovery UX

**Goal:** from the banner, the user re-authenticates and downloads resume.

| Option | What | Verdict |
|--------|------|---------|
| **Deep-link banner → Settings** | Banner routes to `/settings`, where the existing "Connect Qobuz" OAuth flow (`GET /api/auth/qobuz/oauth-url` → redirect callback) or manual-token paste re-captures credentials. Those endpoints already call `_reload_clients`, which reopens sessions and re-resolves the secret. | **Preferred (cheap, reuses shipped flows).** |
| Auto-relogin / silent refresh | Refresh the token without user action. | **Rejected — hard external constraint.** Qobuz exposes **no refresh endpoint** (CLAUDE.md Known Issues; SDK `qobuz/auth.py` has `exchange_code` for OAuth *code* exchange but no refresh). Document this so the next engineer does not re-attempt it. Contrast Tidal, whose SDK refreshes on `__aenter__` and on 401. |

Re-auth already triggers `_reload_clients` (`backend/api/config.py` and the OAuth
callbacks in `backend/api/auth.py`), which reopens sessions and re-runs
`_resolve_qobuz_credentials`. You should not need new backend code here — verify
the banner clears once `/api/auth/status` reports the source authenticated.

**Note on `/api/auth/status`:** it reports `authenticated` by checking that
`client._transport._session is not None` (`backend/api/auth.py`, `auth_status`).
That only proves a session **object exists**, **not that the token works** — a
freshly-expired token still reads as `authenticated: true`. Do **not** use
`auth_status` as your expiry signal; it is a liveness check of the object, not a
token-validity check. The 401-on-real-operation signal from Phase 2 is the source
of truth.

### Phase 4 gate

| Command | Expected | If not → branch |
|---------|----------|------------------|
| After re-auth, retry the download. | Download succeeds; banner clears. | If downloads still 401 after re-auth with a valid OAuth token, you are in secret-resolution territory → Phase 5. |
| `GET /api/auth/status` post-reauth. | `authenticated: true` for qobuz. | Remember this is object-liveness only — confirm with a real download, not this. |

## (5) Phase 5 — Secret-chain hardening (separate go/no-go)

**Do NOT start Phase 5 without reading `qobuz-tidal-domain-reference` first
(hard prerequisite).** You must understand the MD5 signing formula and the two
app identities before touching this code, or you will reintroduce the `8e1f8e2`
class of bug.

### Theory you must hold before editing

- **Two app identities** (verified in `sdks/.../qobuz/auth.py`, 2026-07-03):
  OAuth/Helper app `APP_ID = 304027809` with a hardcoded `APP_SECRET` (a 32-hex
  MD5-style string); web-player app `798273057` (libsync's fallback in
  `backend/main.py` / `backend/api/config.py`) whose secret the spoofer scrapes.
  `PRIVATE_KEY` (a short alphanumeric constant) is the OAuth *code-exchange*
  key, **not** a signing secret — do not confuse them. Read the literal values
  locally with `grep -n 'APP_ID\|APP_SECRET\|PRIVATE_KEY' sdks/qobuz_api_client/clients/python/qobuz/auth.py`;
  never copy secret values into docs, issues, or commits (see `libsync-external-positioning`).
- **Signing formula** (`sdks/.../qobuz/streaming.py`): `get_file_url` signs
  `MD5("trackgetFileUrlformat_id{fid}intent{intent}track_id{tid}{ts}{app_secret}")`;
  `start_session` uses `MD5("qbz-1{ts}{app_secret}")`. A wrong secret produces a
  signed-but-rejected request, which surfaces as a download failure — not always
  a clean 401.
- **Resolution order** (`backend/main.py`, `_resolve_qobuz_credentials`):
  (1) user override `qobuz_app_secret` config key → (2) hardcoded
  `qobuz.auth.APP_SECRET` when the client's `app_id == OAUTH_APP_ID` → (3) spoofer:
  `fetch_app_credentials()` scrapes `play.qobuz.com`, then `find_working_secret()`
  tests each candidate with a **live** quality-2 `get_file_url` call and returns
  the first that works (falls back to the first candidate with a warning if none
  verify).

### Candidate work (all labeled candidate — get go/no-go per item)

| Item | What | Obligation |
|------|------|------------|
| **Observable resolution outcome (satisfies D4)** | Record which tier won (override / hardcoded / spoofer) and the resulting `app_id` into a diagnostic surface. **Backend INFO logging is invisible at runtime** (no logging config is installed — `_resolve_qobuz_credentials` already logs at INFO but you won't see it under uvicorn; pytest does show it). So write the outcome somewhere queryable: a `config` key (e.g. `qobuz_secret_source`) or a diagnostic endpoint — not just a log line. | Must not print the secret value itself; secrets are sensitive (`libsync-external-positioning`). |
| Persist last-verified secret | Cache the secret that `find_working_secret` confirmed so a restart does not re-scrape. | Never overwrite `qobuz_app_id`; only cache when unset (Ground rules). |
| Spoofer failure alerting | When `find_working_secret` raises (no candidate verifies), surface it (e.g. a distinct event/banner) instead of silently using `secrets[0]`. | Reuse the Phase 2/3 event+banner machinery; do not add a probe. |

### Phase 5 gate

| Command | Expected | If not → branch |
|---------|----------|------------------|
| Trigger secret resolution (fresh boot with a token), then query your diagnostic. | Shows the winning tier + `app_id`, no raw secret. | If it prints the secret, redact before merge. |
| `grep -n "qobuz_app_id" backend/` | No new unconditional writes; caching still guarded by "only when unset". | Any unconditional write → revert (8e1f8e2 class). |
| `make test`. | Green. | Fix before proceeding. |

## (6) Validation and promotion

Success is **never judged by eye** — re-check the D1–D5 criteria mechanically.

### Tests required per phase

| Phase | Test |
|-------|------|
| 1 | Injecting test proving the current baseline (`download_failed`, no `token_expired`). |
| 2 | `token_expired` fires once per burst on `AuthenticationError`; `download_failed` unaffected; non-auth errors do **not** trigger it. |
| 3 | Frontend build passes; manual WS-frame observation logged. |
| 4 | Manual re-auth restores downloads (documented). |
| 5 | Diagnostic reports the winning tier; no unconditional `app_id` write; secret never logged raw. |

### Pre-push checklist (matches CI — see `libsync-validation-and-qa`)

Route all changes through a PR with the four required checks — never around them
(see `libsync-change-control`). CI job names (verified in
`.github/workflows/`, 2026-07-03): `backend-tests` + `frontend-build` +
`docker-publish` (all in `pytest.yml`), and `ruff` (in `ruff.yml`).

- [ ] `make test` → 154 baseline passing plus your additions (154 as of 2026-07-03, v0.0.6).
- [ ] `cd frontend && npm run build` succeeds (mirrors `frontend-build`).
- [ ] Ruff clean on your changed files. **Do not run `make lint --fix` blindly** —
      `make lint` currently fails on clean `main` due to a local ruff 0.1.15 vs CI
      version skew; a blanket autofix will reformat unrelated files. Lint only what
      you touched and match CI's ruff version.
- [ ] Submodule pin unchanged unless you took Phase 2 option (b) — if you bumped it,
      run `make deps` and note the bump in the PR (SDK changes are a separate repo).

### Docs to update on merge

- `CHANGELOG.md` — add an entry under a new version section (the file mirrors
  annotated tag messages).
- `docs/WEB_UI.md` — the WebSocket event table currently lists `token_expired`
  among emitted events; once it actually fires, confirm the row is accurate and
  document the banner behavior. (That table also lists `scan_progress`,
  `scan_complete`, `album_status_changed` which are **not** bridged — do not
  "fix" those here; they are out of scope, see the architecture notes.)

### Final acceptance — re-run against D1–D5

Re-execute the Phase-2 test (D1), the Phase-3 WS observation (D2), the Phase-4
re-auth (D3), the Phase-5 diagnostic (D4), and confirm all four CI checks green
(D5). Only then is the campaign done.

## When NOT to use this skill

- **"Only downloads 401, library works fine"** → that is an **app-id / signing
  mismatch**, not token expiry. Use the discriminating experiment in
  `libsync-proof-and-analysis-toolkit` and the app-id model in
  `qobuz-tidal-domain-reference`. This campaign assumes the token itself expired.
- **Understanding codecs, quality tiers, OAuth flows, MD5 signing theory** →
  `qobuz-tidal-domain-reference` (and read it before Phase 5 regardless).
- **A general bug with unknown cause** → `libsync-debugging-playbook`
  (symptom→triage) then `superpowers:systematic-debugging`.
- **What the app-id incidents actually were** → `libsync-failure-archaeology`.
- **Whether a change is even allowed / how it gets reviewed** →
  `libsync-change-control`.
- **Tidal auth issues** → out of scope; Tidal auto-refreshes and is a different
  flow entirely.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it (repo is the source of truth).

| Fact | Re-verify command |
|------|-------------------|
| `token_expired` subscribed but never published/consumed. | `grep -rn token_expired backend/ frontend/src/` (expect only `backend/main.py` + `docs`). |
| The 7 bridged WS event types. | Read the event-type tuple in `backend/main.py` `create_app`. |
| SDK raises `AuthenticationError` on 401. | `sed -n '1,55p' sdks/qobuz_api_client/clients/python/qobuz/errors.py` |
| Download 401 → `download_failed`, album reverts. | Read the `except Exception` block in `backend/services/download.py` `_process_queue`. |
| `auth_status` checks only session-object existence. | Read `auth_status` in `backend/api/auth.py`. |
| Secret resolution order (override → hardcoded → spoofer). | Read `_resolve_qobuz_credentials` in `backend/main.py`. |
| Two app identities + constants. | `grep -n 'APP_ID\|APP_SECRET\|PRIVATE_KEY' sdks/qobuz_api_client/clients/python/qobuz/auth.py` |
| Signing formulas. | `grep -n 'trackgetFileUrl\|qbz-1' sdks/qobuz_api_client/clients/python/qobuz/streaming.py` |
| Never-overwrite-app_id rule + incidents. | `git show cfa4449` and `git show 8e1f8e2` (commit messages). |
| CI check names. | `grep -n '^  [a-z-]*:' .github/workflows/pytest.yml .github/workflows/ruff.yml` |
| Baseline test count. | `make test` (154 passed, ~2s, as of 2026-07-03, v0.0.6). |
| `_reload_clients` closes sessions (why auto-reload is banned). | Read `_reload_clients` in `backend/api/config.py`. |
| Qobuz has no refresh endpoint. | `grep -n 'def ' sdks/qobuz_api_client/clients/python/qobuz/auth.py` (no `refresh_*`). |
| Test template for injecting a fake downloader. | Read `tests/test_download_failure_threshold.py`. |
