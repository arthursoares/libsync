---
name: qobuz-tidal-domain-reference
description: Use when you lack background on how Qobuz/Tidal streaming APIs, audio formats, or reverse-engineered auth work as they apply to libsync — e.g. confused by format IDs 5/6/7/27, MQA vs HI_RES_LOSSLESS, why a download needs an app_secret not just a token, why Tidal HiRes yields MP4-in-.flac without ffmpeg, why X-App-Id 304027809 vs 798273057 matters, what device-code vs PKCE unlocks, TrackResult.path vs .file_path, or why live-API testing is risky.
---

# Qobuz / Tidal domain reference (for libsync)

This is a **theory-and-facts reference**, not a runbook. It gives a zero-context engineer
the streaming-domain background that libsync assumes but never explains. It defines each
term once at first use, then anchors the fact to a repo location you can re-verify.

All facts date-stamped **(as of 2026-07-03, libsync v0.0.6, SDK pin `5e66211`)** are
drift-prone; see **Provenance and maintenance** for re-verification commands.

The SDK code lives in the git submodule at `sdks/qobuz_api_client/clients/python/`
(the `qobuz/` and `tidal/` packages). All SDK paths below are relative to that directory.
Backend paths are relative to the repo root.

---

## 1. The ecosystem: what Qobuz and Tidal are, and why this is fragile

**Qobuz** and **Tidal** are paid subscription music-streaming services that sell
**hi-res** (better-than-CD) audio. Neither publishes an official public API for
*downloading* files — their apps stream to paying subscribers only.

libsync (like the `streamrip` project it forked from) talks to these services using
**reverse-engineered client credentials**: app IDs and signing secrets extracted from
the vendors' own desktop/web clients. Consequences you must internalize:

| Consequence | Why it matters here |
|---|---|
| Credentials can rotate or break at any time | Qobuz can rotate the signing secret; Tidal can invalidate a baked-in client ID. There is no vendor contract. |
| Live-API calls consume a real account | Hammering endpoints or automated login attempts can flag or lock the subscriber account used for testing. |
| The SDK is a separate repo | Downloader/auth logic lives in `arthursoares/qobuz_tidal_api_client`, consumed as a submodule — not vendored. Bugs may need an upstream SDK fix + a pin bump. |

**Testing rule:** unit tests (`make test`, ~154 tests, ~8s) use no credentials and hit no
network. Anything that reaches live Qobuz/Tidal (real downloads, OAuth polling, spoofer
scrapes) is **not** safe to run casually. For live recovery work see
`libsync-qobuz-auth-campaign`; for the history of past auth breakages see
`libsync-failure-archaeology`.

---

## 2. Audio formats: the vocabulary

- **Lossy** (MP3, AAC): audio compressed by discarding data you supposedly can't hear.
  Smaller files, not bit-exact. Qobuz's lowest tier is MP3 320 kbps; Tidal's are AAC.
- **Lossless** (FLAC): compressed but bit-exact — decodes back to the original PCM.
- **Bit depth / sample rate**: two independent quality axes.
  - **16-bit / 44.1 kHz** = CD quality ("Red Book").
  - **24-bit / 96 kHz** or **24/192** = "hi-res" — more dynamic range and bandwidth.
  - libsync stores these per-album as `bit_depth` (INTEGER) and `sample_rate` (REAL)
    columns, added in schema v2 (`backend/models/database.py`, `_migrate_to_v2`).

- **MQA (Master Quality Authenticated)**: a format Tidal marketed as "hi-res" that is
  **physically a 16/44.1 FLAC carrying MQA-encoded subbands** — hi-res information is
  *folded* into a lossy container. It is **not** true hi-res on the wire. In the SDK this
  is Tidal quality tier **3 = `HI_RES`** (see the comment block in `tidal/tidal/types.py`).
  True 24-bit is a *different, newer* tier — see §3.

- **DASH / MPD**: **Dynamic Adaptive Streaming over HTTP** — audio delivered as many small
  segments described by an MPD (manifest) file. Tidal's true-hi-res tier
  (`HI_RES_LOSSLESS`) ships **FLAC audio inside MP4 fragments**. The SDK must **remux**
  (re-wrap the stream without re-encoding) those fragments into a native `.flac` file
  using **ffmpeg** (`ffmpeg -c:a copy`, in `tidal/tidal/downloader.py` `_remux_mp4_to_flac`).

  **If ffmpeg is not on PATH**, `_remux_mp4_to_flac` logs a warning, returns `False`, and
  leaves an **MP4-wrapped FLAC with a `.flac` extension** — a file that strict tag
  readers/scanners will reject as not-really-FLAC. The Docker image installs ffmpeg for
  exactly this reason (`docker/Dockerfile`).

---

## 3. Quality tiers: two separate vocabularies, and the "double cap"

libsync has **one** config knob per source; each SDK maps it onto **its own** scale.

### libsync config → SDK tier
The backend reads config key `qobuz_quality` or `tidal_quality`, **default `3`**, fresh on
every download (`backend/services/download.py`, `quality_key = f"{source}_quality"`).

### Qobuz scale (SDK `qobuz/streaming.py`, `QUALITY_MAP`)
`QUALITY_MAP = {1: 5, 2: 6, 3: 7, 4: 27}` — the config tier maps to a Qobuz **format ID**:

| Config tier | Format ID | Meaning |
|---|---|---|
| 1 | 5 | MP3 320 (lossy) |
| 2 | 6 | FLAC 16/44.1 (CD) |
| 3 (default) | 7 | FLAC 24-bit, up to 96 kHz |
| 4 | 27 | FLAC 24-bit, up to 192 kHz |

### Tidal scale (SDK `tidal/tidal/types.py`, `QUALITY_MAP`)

| Config tier | String | Meaning |
|---|---|---|
| 0 | `LOW` | AAC ~96 kbps |
| 1 | `HIGH` | AAC ~320 kbps |
| 2 | `LOSSLESS` | FLAC 16/44.1 (CD) |
| 3 (default) | `HI_RES` | **MQA-folded FLAC — legacy "HiRes", not true hi-res** (§2) |
| 4 | `HI_RES_LOSSLESS` | **True 24-bit FLAC, up to 192 kHz, DASH-delivered** |

### The double cap (Tidal)
Requesting tier 4 does **not** guarantee 24-bit audio. Two independent ceilings apply:

1. **OAuth client entitlement.** A **device-code** token is capped at roughly AAC 320 kbps
   regardless of the tier you ask for; the **PKCE** token is what unlocks
   `LOSSLESS`/`HI_RES`/`HI_RES_LOSSLESS` (see §5 and the `tidal/tidal/auth.py` comments).
2. **Subscription tier.** The account itself must be entitled to hi-res.

So "I set `tidal_quality=4` but got AAC" usually means a device-code token, not a bug. And
`HI_RES_LOSSLESS` additionally needs ffmpeg for the remux (§2).

---

## 4. Qobuz auth model: two app identities, and why the *secret* matters

Qobuz identifies the calling app with an **App ID** sent as the `X-App-Id` HTTP header.
Downloads additionally require **signing** each stream-URL request with an **App Secret**.
A token alone is not enough — you also need the matching secret.

### Two app identities (both are real, load-bearing constants)

| App ID | Role | Secret source |
|---|---|---|
| `304027809` | OAuth / desktop "Helper" app | Hardcoded `APP_SECRET` constant (a 32-hex string) in `qobuz/auth.py` (decoded from the desktop Helper bundle) — do not quote its value; retrieve it locally via the Provenance grep |
| `798273057` | Web-player app (used with a manually pasted web token) | **Scraped live** from `play.qobuz.com` by the SDK spoofer (`qobuz/spoofer.py`) |

`qobuz/auth.py` also defines a `PRIVATE_KEY` constant — this is the **OAuth
code-exchange key, NOT the signing secret** (the source comment says so explicitly). Do not
confuse the three constants. Per house rules (`libsync-external-positioning`), refer to
these secrets by constant name and file path only — never paste their values into docs,
issues, or commits. The Provenance grep below retrieves them locally when needed.

**`X-App-Id` must match the app that issued the token.** libsync enforces this:
- The OAuth flow persists `qobuz_app_id=304027809` (`backend/api/auth.py`).
- A manual `qobuz_token` PATCH with no explicit app_id auto-pins `qobuz_app_id=798273057`
  (`backend/api/config.py`).
- The default fallback app_id when nothing is stored is `798273057` (`backend/main.py`).

> Header timing (inferred — confirm with maintainer): the SDK uses `aiohttp`, which bakes
> session headers at session construction, and Qobuz appears to validate `X-App-Id` on
> only some endpoints. Treat this as a plausible failure mode, not a documented guarantee.

### Stream-URL request signing (the reason the secret is needed)
`qobuz/streaming.py` `_compute_signature` builds the download signature as:

```
MD5("trackgetFileUrlformat_id{format_id}intent{intent}track_id{track_id}{timestamp}{app_secret}")
```

`get_file_url` raises `ValueError` if `app_secret` is `None`. A separate formula signs
`start_session`: `MD5("qbz-1{timestamp}{app_secret}")`. This is **why the SECRET, not just
the token, gates downloads.**

### Backend secret resolution order
`_resolve_qobuz_credentials` in `backend/main.py` resolves the secret in this order:
1. user override config key `qobuz_app_secret`;
2. if the token's app_id `== qobuz.auth.APP_ID` (`304027809`), use the hardcoded
   `qobuz.auth.APP_SECRET`;
3. otherwise the **spoofer** path — `fetch_app_credentials()` scrapes candidates from
   `play.qobuz.com`, then `find_working_secret(...)` tests each against a real
   quality-2 `get_file_url` call and returns the first that works.

`backend/main.py` wraps `from qobuz.auth import APP_SECRET` in `try/except ImportError` so
an **older** SDK pin (before the constant existed) silently falls through to the spoofer
instead of crashing. Bumping the pin backward therefore changes auth behavior quietly.

### No refresh endpoint
Qobuz has **no** token-refresh endpoint. Expiry means the user must re-authenticate via the
OAuth flow (this matches CLAUDE.md's "Known Issues"). For operational recovery see
`libsync-qobuz-auth-campaign`.

---

## 5. Tidal auth model: device-code vs PKCE, and auto-refresh

Tidal offers **two** OAuth flows; the SDK implements both in `tidal/tidal/auth.py`.

| Flow | User action | Client ID (base64-decoded in SDK) | Unlocks |
|---|---|---|---|
| **Device-code** | Enter a code at `link.tidal.com` | `CLIENT_ID` | AAC only (~320 kbps cap) |
| **PKCE** | Paste a redirect URL from `tidal.com/android/login/auth` | `CLIENT_ID_PKCE` | `LOSSLESS` / `HI_RES` / `HI_RES_LOSSLESS` |

**PKCE** = "Proof Key for Code Exchange", an OAuth extension where the client generates a
secret **verifier** and sends only its hash to start; the verifier proves identity at code
exchange. In libsync the verifier is held **in memory server-side** in a module-level dict
(`_pkce_pending`, `backend/api/auth.py`) keyed by an opaque handle — **lost on restart,
never expired**. If a PKCE flow is interrupted by a backend restart, the user must restart it.

**Which refresh path is used** is selected by the `tidal_auth_method` marker in the stored
credentials (config key, default `device_code`; set to `pkce` by the PKCE-complete endpoint
in `backend/api/auth.py`).

**Auto-refresh** (SDK `tidal/tidal/client.py`): on `__aenter__` the client calls
`ensure_token()`, which refreshes if the token expires within `refresh_window_seconds`
(default `86400` = 24h); it also wires a 401-retry hook via
`set_refresh_callback(self._force_refresh)` when `auto_refresh` is on and a refresh token is
present. This is why Tidal "just works" across restarts and Qobuz does not.

> Note: refreshed Tidal tokens are **not** written back to the config DB — the SDK refreshes
> in memory each boot from the stored `refresh_token`. Harmless as long as the refresh token
> stays valid. (Verified: `set_config("tidal_access_token", ...)` appears only in the auth
> endpoints.)

### PKCE helpers are not top-level exports
`generate_pkce_pair`, `build_pkce_authorize_url`, `extract_code_from_redirect`,
`exchange_pkce_code`, `refresh_pkce_token` are **not** in the tidal package `__all__` — only
the device-code helpers are. Import PKCE functions from `tidal.auth` directly, exactly as
`backend/api/auth.py` does. Importing them from the top-level `tidal` package will fail.

---

## 6. The SDK facade contract (what libsync relies on)

Both `qobuz` and `tidal` expose a **matching facade** so most library/sync/download code is
source-agnostic (it branches with `hasattr(client, 'catalog')`).

| Surface | Qobuz | Tidal |
|---|---|---|
| `client.catalog` | yes | yes |
| `client.favorites` | yes (read + add/remove) | yes (**read-only**) |
| `client.streaming` | yes | yes |
| `client.playlists` | yes | **no** |
| `client.discovery` | yes | **no** |
| Default `requests_per_minute` | `30` | `240` |

`AlbumDownloader` has an **identical** signature in both packages:
`__init__(client, config, on_track_start=(num,title), on_track_progress=(num,bytes_done,bytes_total), on_track_complete=(num,title,success))` and `await download(album_id)`.
`DownloadService._download_album` (`backend/services/download.py`) wires those three sync
callbacks to WebSocket `download_progress` events.

### Divergences that will bite you
These are the places where the two SDKs are **not** identical:

| Thing | Qobuz | Tidal | Guard in libsync |
|---|---|---|---|
| `TrackResult` file path field | `.path` | `.file_path` | Backend reads only `.track_id`/`.success`/`.title`, so it is safe **today**. Any new code touching the path must branch by source. |
| `DownloadConfig` extra field | `download_booklets` | `write_metadata_file` | Backend adds `download_booklets` to kwargs only when `source == "qobuz"` (`download.py`). |
| `DownloadConfig.output_dir` | defaults to `"."` | **required, no default** | Backend always passes `output_dir` for Tidal. |
| PKCE auth helpers | n/a | not in `__all__` | Import from `tidal.auth` (§5). |

Passing a Qobuz-only `DownloadConfig` key (e.g. `download_booklets`) to Tidal's
`DownloadConfig` raises `TypeError`. `AlbumResult` exposes `.total`/`.successful`/
`.success_rate` on both (computed `@property` on Qobuz, constructor fields on Tidal), so
reading them is safe, but **constructing** one by hand differs between SDKs.

---

## 7. Dedup and the per-album sentinel

libsync avoids re-downloading via two mechanisms.

**1. Per-source dedup DB** — a one-table SQLite file, `downloads(id TEXT PRIMARY KEY)`,
holding track IDs already downloaded:
- Qobuz → `data/downloads.db` (legacy name preserved).
- Tidal → `data/downloads-tidal.db` (so IDs from the two services can't collide).
- The dir is `dirname(STREAMRIP_DB_PATH)` — **not** the downloads path (`download.py`).
- Passed to the SDK as `downloads_db_path`; the SDK downloader also writes it during real
  downloads. When `force=True`, the backend sets it to `None` to bypass the skip.
- **Safe to delete** — you only lose skip-already-downloaded state.

**2. Per-album `.streamrip.json` sentinel** — a small JSON file written into each album
folder. Written by both SDK downloaders during download AND, best-effort, by
`mark_album_downloaded` (`backend/services/scan.py`, `_write_sentinel`). Payload
(`_sentinel_payload` in `scan.py`):

```json
{"source": "...", "album_id": "...", "title": "...", "artist": "...",
 "tracks_count": <int|null>, "downloaded_at": "..."}
```

The fuzzy scan **short-circuits** any folder that already has a `.streamrip.json`.
**Safe to delete** — the scan re-examines the folder and re-marks it.

`mark_album_downloaded` / `unmark_album_downloaded` in `scan.py` are the canonical
**scan-path** writers of (a) `albums.download_status`, (b) the dedup DB, and (c) the
sentinel. Note the invariant is **narrower than CLAUDE.md implies**: the download service
and the legacy `POST /api/downloads/scan` endpoint also write `albums.download_status`
directly. The "only place" claim holds only within the scan / manual-mark path. Deeper
detail on that discrepancy belongs to `libsync-architecture-contract`.

---

## 8. Rate limits: why you must not hammer

Each SDK client throttles itself with a requests-per-minute limiter:

| Source | Default `requests_per_minute` (SDK client constructor) |
|---|---|
| Qobuz | `30` |
| Tidal | `240` |

These defaults exist because the services **can flag or throttle** an account that issues
too many requests too fast — and because every request rides reverse-engineered credentials
(§1), a flagged account is a real, hard-to-recover cost. Do not raise these limits or add
retry storms without understanding that the downside lands on a live subscriber account.

---

## When NOT to use this skill

This skill is **background theory**. Use a sibling instead when you need to *act*:

- Concrete config keys, defaults, precedence, env vars → **libsync-config-and-flags**.
- Recovering a broken Qobuz login / secret in production → **libsync-qobuz-auth-campaign**.
- The history of past auth/format breakages and dead ends → **libsync-failure-archaeology**.
- Load-bearing design invariants and the "only place" nuance → **libsync-architecture-contract**.
- A symptom you need to triage (e.g. "downloads produce AAC") → **libsync-debugging-playbook**.
- Running, deploying, or the data-artifact map → **libsync-run-and-operate**.

---

## Provenance and maintenance

Every fact below is drift-prone. Re-verify with these copy-pasteable commands (run from the
repo root; SDK path prefix is `sdks/qobuz_api_client/clients/python`).

| Fact | Re-verify |
|---|---|
| SDK submodule pin (`5e66211` as of 2026-07-03) | `git submodule status sdks/qobuz_api_client` |
| SDK remote repo | `grep url .gitmodules` |
| Qobuz quality map `{1:5,2:6,3:7,4:27}` | `grep -n QUALITY_MAP sdks/qobuz_api_client/clients/python/qobuz/streaming.py` |
| Tidal quality map (LOW..HI_RES_LOSSLESS) | `grep -n -A8 QUALITY_MAP sdks/qobuz_api_client/clients/python/tidal/tidal/types.py` |
| Qobuz signing formula | `grep -n -A4 'def _compute_signature' sdks/qobuz_api_client/clients/python/qobuz/streaming.py` |
| `start_session` formula (`qbz-1...`) | `grep -n 'qbz-1' sdks/qobuz_api_client/clients/python/qobuz/streaming.py` |
| Qobuz constants APP_ID/APP_SECRET/PRIVATE_KEY | `grep -n 'APP_ID\|APP_SECRET\|PRIVATE_KEY' sdks/qobuz_api_client/clients/python/qobuz/auth.py` |
| Backend secret resolution order | `grep -n '_resolve_qobuz_credentials\|APP_SECRET\|find_working_secret' backend/main.py` |
| Backend app_id defaults / pinning | `grep -rn '798273057\|304027809' backend/main.py backend/api/config.py backend/api/auth.py` |
| Spoofer scrape + working-secret test | `grep -n 'fetch_app_credentials\|find_working_secret\|test_track_id' sdks/qobuz_api_client/clients/python/qobuz/spoofer.py` |
| Tidal client IDs (device vs PKCE) | `grep -n 'CLIENT_ID\|CLIENT_ID_PKCE' sdks/qobuz_api_client/clients/python/tidal/tidal/auth.py` |
| Tidal auto-refresh window (86400s) | `grep -n 'ensure_token\|refresh_window_seconds\|set_refresh_callback' sdks/qobuz_api_client/clients/python/tidal/tidal/client.py` |
| PKCE helpers absent from `__all__` | `grep -n -A20 '__all__' sdks/qobuz_api_client/clients/python/tidal/tidal/__init__.py` |
| ffmpeg remux behavior (DASH→FLAC) | `grep -n 'ffmpeg\|_remux_mp4_to_flac' sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py` |
| TrackResult `.path` vs `.file_path` | `grep -n 'path\|file_path' sdks/qobuz_api_client/clients/python/qobuz/downloader.py sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py` |
| DownloadConfig field divergence | `grep -n 'class DownloadConfig\|output_dir\|download_booklets\|write_metadata_file' sdks/qobuz_api_client/clients/python/qobuz/downloader.py sdks/qobuz_api_client/clients/python/tidal/tidal/downloader.py` |
| requests_per_minute defaults (30 / 240) | `grep -rn requests_per_minute sdks/qobuz_api_client/clients/python/qobuz/client.py sdks/qobuz_api_client/clients/python/tidal/tidal/client.py` |
| Sentinel payload shape | `grep -n -A9 '_sentinel_payload' backend/services/scan.py` |
| Dedup DB filenames / dir | `grep -n 'downloads.db\|downloads-\|downloads_db_path' backend/services/download.py` |
| Backend quality-key read (default 3) | `grep -n '_quality\|quality_key' backend/services/download.py` |

**Confirm-with-maintainer items** (house inferences the docs do not state):
- The `aiohttp` header-timing / partial `X-App-Id` validation description in §4 is an
  inference about *why* app-id mismatches fail, not a documented guarantee.
