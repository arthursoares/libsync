---
name: libsync-failure-archaeology
description: "Use when a libsync symptom looks familiar — 'library loads but downloads 401', wrong quality/folder labels, boolean settings behaving inverted, scan progress stuck at '0 / ?', 'Album <id>' placeholder names — or when git history confuses: a bisect stops at root f5058e4, a submodule pin does not exist upstream, PRs #7–#10 show MERGED but their content is absent from main, CodeQL flags path-injection on serve_frontend, or a 22-bug review file cites nonexistent streamrip/ paths."
---

# Libsync Failure Archaeology

This skill is the chronicle of every major investigation, dead end, rejected fix, and settled battle in this repository, so nobody re-fights them. Each entry follows the same shape: symptom, root cause, evidence, status. Every entry was re-verified against actual commits (`git show <sha>`) on 2026-07-03 at v0.0.6.

Read the entry that matches your symptom before proposing any fix. If an entry says SETTLED, the fight is over — do not reopen it without new evidence. If it says OPEN or PARTIALLY SETTLED, the residue described is real work.

Status legend:

| Status | Meaning |
|---|---|
| SETTLED | Fixed or deliberately decided. Do not re-litigate. |
| PARTIALLY SETTLED | The main fix landed but named residue remains. |
| OPEN | Known, unresolved, and nobody is working on it. |

A grep warning that applies to every entry: repository-root greps also match `.claude/worktrees/agent-*/` (full copies of deleted CLI-era code), `node_modules`, `.svelte-kit`, and `sdks/`. Exclude those directories unless you intend to search them.

---

## 1. THE REROOT (2026-04-14) — main's history was rewritten

**Symptom:** `git bisect` or `git log` bottoms out at commit `f5058e4` ("Add browse-library feature", dated 2025-08-13) and you cannot find any upstream `nathom/streamrip` history, even though the project is documented as a fork of it.

**Root cause:** On 2026-04-14 the maintainer rewrote main's history with `git replace` plus `filter-branch`, cutting off 831 commits of upstream `nathom/streamrip` ancestry. The maintainer's first own commit became the new root. This was deliberate; the rewritten commits are byte-identical to the originals except that the root's parent line was dropped.

**Evidence (all commands verified 2026-07-03; the first three only work in the maintainer's original clone):**

```bash
git for-each-ref refs/replace refs/original   # shows refs/replace/eeee774... -> f5058e4
git --no-replace-objects cat-file -p eeee774  # true parent: 5bcadd3 (upstream streamrip)
git --no-replace-objects log --oneline eeee774 | wc -l   # 831 hidden upstream commits
git merge-base main pre-reroot-backup-20260414-170428    # exits 1: histories are disjoint
```

**Implications for you:**

- The branch `pre-reroot-backup-20260414-170428` and the `refs/replace` ref exist ONLY in the maintainer's local clone. A fresh clone from GitHub sees `f5058e4` as the true root and none of these commands work.
- Never bisect across `f5058e4` expecting upstream history; it is not there.
- In the maintainer's clone, `git log` silently follows the replace ref when viewing the backup branch. Use `git --no-replace-objects` to see true ancestry.
- The motivation for the reroot is not recorded in the repo (inferred — confirm with maintainer).

**Status: SETTLED** (deliberate). Re-check: `git log --reverse --format='%h %s' | head -1` should print `f5058e4`.

---

## 2. THE QOBUZ APP-ID SAGA (2026-04-09) — five commits in one afternoon

**Symptom:** Qobuz library and favorites load fine, but album detail, sync, and downloads return HTTP 401. Or: downloads that worked for web-player-token users suddenly break after an "app_id fix".

Definitions: an **app_id** is the client-application identifier Qobuz binds each auth token to, sent as the `X-App-Id` HTTP header. An **app_secret** is the per-app key used to sign `get_file_url` download requests. Qobuz has two relevant app identities: `304027809` (the OAuth/desktop-Helper app) and `798273057` (the web-player app, whose secret a "spoofer" scrapes from play.qobuz.com).

**Root cause (three interacting facts):**

1. `aiohttp.ClientSession` bakes headers at session construction, so an app_id learned after boot never reaches outgoing requests unless the session is rebuilt.
2. Qobuz validates `X-App-Id` on `album/*` and download endpoints but NOT on `favorites/*` — hence the confusing "library works, downloads 401" split. (As observed during the 2026-04-09 incident and recorded in commit `6650993`; live Qobuz behavior may have changed and cannot be re-verified offline. The operative test remains which read tier fails: favorites-level reads succeeding while album-detail reads 401 is the discriminating signal.)
3. Tokens are strictly bound to the app that issued them, and each app has a different signing secret.

**The commit-by-commit story (read the full messages with `git show --no-patch <sha>`):**

| Commit | What it did | Outcome |
|---|---|---|
| `6650993` | Rebuilt the aiohttp session with the real app_id after fetching bundle credentials (boot fallback was `950096963`). | Fixed the stale-header half; set up the next bug. |
| `8e1f8e2` | Forced `OAUTH_APP_ID` (`304027809`) unconditionally, overwriting any cached value. Its own message admits "a previous fix made it worse". | Broke every user with a web-player-scoped token. |
| `cfa4449` | Reverted the unconditional overwrite; default to `798273057`; only cache an app_id when the DB has none. | Established the rule below. |
| `1e9db8e` | Added `qobuz_app_id` / `qobuz_app_secret` user overrides in Settings. | Escape hatch. |
| `e4e0a1f` | Hardcoded `qobuz.auth.APP_SECRET` (decoded from the Qobuz desktop Helper bundle) for OAuth-bound clients. | OAuth browse + download finally worked end-to-end. Final resolution. |

**The settled rule:** never unconditionally overwrite a cached `qobuz_app_id`. The OAuth callback persists its own app_id; the resolver (`backend/main.py`, `_resolve_qobuz_credentials`) may only fill an empty value. Users with manually-pasted web-player tokens (`798273057`) take the spoofer code path; OAuth tokens (`304027809`) use the hardcoded SDK secret. Any change that collapses these two paths repeats `8e1f8e2`.

**Status: SETTLED.** Re-check: `git log --oneline 6650993..e4e0a1f` and `grep -n "APP_SECRET" backend/main.py`.

---

## 3. QUALITY-TIER BUG FAMILY (recurring) — two vocabularies per source

**Symptom:** downloads fail outright, or download at the wrong quality, or land in folders whose `[FLAC-24-192]`-style label does not match the actual audio inside.

**Root cause pattern:** every source has TWO different quality vocabularies — an internal tier level and a service-native format identifier or entitlement — and each bug in this family confused one for the other. Each instance is fixed; the pattern is a live trap for any new quality-touching code.

| Instance | Commit / record | Confusion | Fix |
|---|---|---|---|
| Settings sent Qobuz format IDs 5/6/7/27 where levels 1–4 were expected. Commit message: "root cause of all download failures" (2026-04-07). | `1bcb080` | Format ID vs tier level | Settings now use levels 1–4; internal map is `(5, 6, 7, 27)`. |
| Settings never exposed `tidal_quality`, so Tidal always downloaded at the schema default of 3 regardless of user intent (2026-04-18). | `c0e5d96` | Backend read a key the UI never wrote | Tidal quality `<select>` added, mirroring the Qobuz pattern. |
| Tidal folders labeled `[FLAC-…]` while the files were AAC: `_tidal_quality_fields` used the album's max-available tier instead of `min(album_cap, user_request)` (v0.0.5). | CHANGELOG.md, v0.0.5 "Fixed" | Album capability vs actual download tier | Compute `min(album_cap, user_request)`. |
| Qobuz folder labeled `[24B-192kHz]` for a CD-quality download of a hi-res album (v0.0.5). | CHANGELOG.md, v0.0.5 "Fixed" | Album max vs requested tier | Label mirrors the requested tier. |

**Status: each instance SETTLED; the family pattern remains a live trap.** When writing quality code, state explicitly which vocabulary each variable holds. Re-check: `git show --no-patch 1bcb080 c0e5d96` and `grep -n "min(" backend/services/download.py`.

---

## 4. CODEX-REVIEW CLUSTER `bf293f7` (2026-04-10) — four findings, one commit

An external Codex review of PR #1 produced two P1 and two P2 findings, all fixed in `bf293f7`. Each is a distinct trap worth knowing:

| Finding | Root cause | Fix | Residue |
|---|---|---|---|
| P1: CI imports failed on clean runners. | Checkout steps lacked `submodules: recursive`, so `from qobuz import ...` failed. | Added to all checkout steps in `.github/workflows/pytest.yml` plus SDK `pip install -e`. | Any NEW workflow file must repeat this. |
| P1: auto-sync diff was always empty. | `run_sync` called `refresh_library()` (which upserts all favorites into the DB) BEFORE `get_diff()`, so the diff compared the DB against itself. | `refresh_library` now returns `new_album_ids` directly; the post-refresh diff was removed. | Ordering trap: never diff after upsert. |
| P2: download settings required a restart. | `DownloadService` captured `downloads_path` / `max_connections` in `__init__` and never re-read them. | Read from the config DB at download time. | Pattern trap for any new service holding config. |
| P2: cancel during an active download was silently ignored. | No re-check of `_cancel_requested` after the `await` on `_download_album` returned. | Post-await check marks the item cancelled and reverts album status. | See below. |

**Soft-cancel is documented behavior, not a bug:** the SDK downloaders cannot be interrupted mid-flight, so files still land on disk after a cancel; the cancel only prevents the DB from recording the album as successfully downloaded. This trade-off is stated in `bf293f7`'s own commit message. Do not file it as a defect and do not "fix" it by deleting files without a design discussion.

**Status: SETTLED.** Re-check: `git show --no-patch bf293f7` and `grep -c "submodules: recursive" .github/workflows/pytest.yml` (2 as of 2026-07-03).

---

## 5. PATH-TRAVERSAL P1 `fe43493` AND THE PR #13 CODEQL AFTERMATH

**Symptom (original bug):** the SPA catch-all route in `backend/main.py` (`serve_frontend`) allowed `GET /../secret` to read arbitrary files the process could access. Found as a Codex P1 on PR #1.

**Fix:** `fe43493` (2026-04-14, shipped in v0.0.2) resolves the requested path with `os.path.realpath` and rejects anything whose realpath escapes the static root via a `startswith(static_root + os.sep)` guard, falling back to `index.html`. The guard sits at `backend/main.py:344-351` (line numbers as of 2026-07-03, v0.0.6).

**The aftermath:** CodeQL's taint tracker does not recognize the `startswith` pattern as a sanitizer, so it kept raising `py/path-injection` alerts (#3, #4, and #8 — the last re-fired when PR #12's `ruff format` moved line numbers). PR #13 (`cb7bab7`, branch `fix/static-serve-path-harden`) rewrote the functionally-correct guard as `Path.resolve()` + `relative_to()` purely to satisfy CodeQL — and was CLOSED unmerged. Main keeps the `startswith` pattern.

**What this means for you:** expect a persistent CodeQL alert on `serve_frontend`. Treat it as a false positive on a verified guard, not a live vulnerability — `tests/test_static_serving.py` covers one- and two-level traversal. Whether the alert is formally dismissed in the GitHub UI, and why PR #13 was closed rather than merged, is not recorded in the repo (inferred — confirm with maintainer before re-opening a rewrite).

**Status: SETTLED** (guard in place; CodeQL appeasement rejected). Re-check: `git show --no-patch fe43493 cb7bab7`, `gh pr view 13 --json state`, `grep -n "startswith" backend/main.py`.

---

## 6. BOOL STRINGIFICATION `c29f2ea` (2026-04-14) — "True" is not "true"

**Symptom:** a boolean toggle in Settings behaves inverted — disabling `embed_artwork` still embeds covers; enabling `source_subdirectories` still writes flat folders — with no error anywhere.

**Root cause:** config values are persisted through Pydantic as `str(value)`, so booleans land in the DB as `"True"` / `"False"` (capital first letter). The download path compared against lowercase `"true"` / `"false"` literals, silently inverting every toggle.

**Fix:** `c29f2ea` added the `_parse_bool` helper in `backend/services/download.py` (accepts both casings) and routed the four affected keys (`embed_cover`, `source_subdirectories`, `disc_subdirectories`, `download_booklets`) through it.

**Remaining residue (verified 2026-07-03):** `backend/api/library.py` still compares `scan_sentinel_write_enabled` with a case-sensitive `== "True"` at lines 111-112 and 161-162. A value stored as lowercase `"true"` (for example, written by hand or by a non-Pydantic client) would silently read as False and skip writing the `.streamrip.json` sentinel. `backend/main.py:128` (auto-sync enable) is case-insensitive via `.lower() not in ("true", "1", "yes")` — three different bool-parsing idioms coexist.

**Rule going forward:** any new code reading a boolean config key must use `_parse_bool` or a case-insensitive comparison. Never compare against a bare lowercase literal.

**Status: PARTIALLY SETTLED.** Re-check: `grep -rn '== "True"' backend --include='*.py'` (excluding tests, expect only the two `api/library.py` sites until someone fixes them).

---

## 7. SCAN UI STUCK AT "0 / ?" — `00e2a19` / PR #14 (2026-04-19)

**Symptom:** the library scan runs to completion, but the UI shows "Scanning… 0 / ?" the whole time and only refreshes when the job finishes.

**Root cause:** two independent transport paths existed. `run_scan` emitted `scan_progress` events into the WebSocket event bus correctly, but the UI polls `GET /api/library/scan-fuzzy/{job_id}`, and that endpoint returned only `{"status": "running"}` with no `scanned`/`total` fields.

**Fix:** the `/scan-fuzzy` POST handler now wraps the event bus so `scan_progress` events also update the in-memory job registry (`app.state.scan_jobs[job_id]["progress"]`); the GET endpoint reads that. WebSocket subscribers still receive the original events. A regression test in `tests/test_api_library_scan.py` polls a slowed scan and asserts `scanned`/`total` are present integers.

**Bonus finding in the same PR:** `ScanReview.svelte` referenced design tokens that do not exist (`--surface`, `--fg`, `--muted`, `--shadow-color`), so CSS resolved them to empty and the panel rendered with no background. Replaced with real tokens from `frontend/src/lib/design-system/tokens.css`. Lesson: this design system does not fail loudly on unknown tokens — verify token names against `tokens.css`.

**Status: SETTLED.** Re-check: `git show --no-patch 00e2a19`.

---

## 8. PLACEHOLDER ALBUM NAMES — `4d219d2` (v0.0.5, 2026-04-27)

**Symptom:** albums appear in the library titled literally `Album <id>`, permanently; separately, Search's "Load More" button disappears after page 1.

**Root cause (two bugs in one commit):**

1. `_fetch_album_metadata` silently fell back to an `"Album <id>"` placeholder when the SDK metadata round-trip failed, then persisted that placeholder to the DB where it stuck.
2. When the Qobuz SDK returned `total=None`, `getattr(result, "total", default)` leaked `None` into the JSON response as `total: null`; the frontend's `??` fallback then used the page size, and `total > results.length` evaluated false, hiding "Load More".

**Fix:** the placeholder fallback was replaced with a fail-loud raise; search results now forward the full title/artist/cover payload alongside album IDs when enqueueing, so the backend prefers the supplied metadata over a re-fetch; the `total` leak got an explicit `None` check.

**Lesson:** in this codebase, silent fallbacks that persist to the DB are treated as bugs — prefer fail-loud. Also, `getattr(x, "total", default)` does not protect against an attribute that exists but is `None`.

**Status: SETTLED.** Re-check: `git show --no-patch 4d219d2` and `grep -rn "Album <" backend/ --include='*.py'` (expect no placeholder construction).

---

## 9. THE STRANDED `dev` BRANCH — merged on GitHub, absent from main

**Symptom:** GitHub shows PRs #7–#10 as MERGED, yet main has no `CONTRIBUTING.md`, no `SECURITY.md`, no multi-arch Docker publish, and no HEAD-request handling on the SPA route. ARM users pulling ghcr images may find amd64-only images.

**Root cause:** PRs #7–#10 were merged into a `dev` branch (base `dev`, verified via `gh pr list --json baseRefName`) that was abandoned after v0.0.2 and never forward-merged to main. Content stranded on `origin/dev` and absent from every release since:

- Multi-arch Docker publish (linux/amd64 + arm64) and HEAD-on-SPA-route handling (PR #10, `514ecc7`)
- `CONTRIBUTING.md`, `SECURITY.md`, issue/PR templates, dependabot config (PR #8)
- Documentation cleanup (PR #9) and a ruff-debt cleanup (PR #7, later redone on main as PR #12)

**Evidence:** `git log main..origin/dev --oneline` shows 11 commits; `ls CONTRIBUTING.md SECURITY.md` fails on main; `grep -c platforms .github/workflows/pytest.yml` prints 0 (all verified 2026-07-03).

**What to do:** if you need any of that content, forward-port it from `origin/dev` via a normal PR against main — never cherry-pick it silently, and never base new work on `dev`. Whether forward-porting is wanted at all is undecided (inferred — confirm with maintainer).

**Status: OPEN.** Re-check: `git log main..origin/dev --oneline | wc -l` (11 as of 2026-07-03).

---

## 10. SDK SUBMODULE HISTORY REWRITES ×2 — old pins may not exist upstream

**Symptom:** `make deps` (which runs `git submodule update --init --recursive`) fails on a checkout of an old libsync commit because the pinned SDK SHA cannot be fetched.

**Root cause:** the `sdks/qobuz_api_client` submodule's upstream repo had its history rewritten twice:

1. **Author-email fix** (re-pinned in libsync commit `52837cd`, 2026-04-14): the author email was corrected on all 35 SDK commits, changing every SHA. The commit message states outright that the old pin `f105a13` "no longer exists upstream".
2. **Rename + made public** (libsync commit `b57a0d2`, 2026-04-14): the repo was renamed `qobuz_api_client` → `qobuz_tidal_api_client` and made public, because the private submodule was breaking the `backend-tests` CI check on every PR.

**Implications:**

- Checking out a pre-2026-04-14 libsync commit and running `make deps` may fail unless the old SDK objects survive in a local reflog. This is expected, not a regression.
- The submodule PATH stays `sdks/qobuz_api_client` despite the repo rename — path and repo name intentionally differ. Do not "fix" the path.
- `docs/WEB_UI.md` still links the old repo name; `.gitmodules` has the real URL (`https://github.com/arthursoares/qobuz_tidal_api_client.git`).

**Status: SETTLED** (deliberate rewrites; permanent hazard for archaeology). Re-check: `git show --no-patch 52837cd b57a0d2` and `git submodule status` (pin `5e66211` as of 2026-07-03, v0.0.6).

---

## 11. DELETED SUBSYSTEMS — do not resurrect

Each of these was deliberately removed. Copies still exist in git history and in `.claude/worktrees/agent-*/` directories, which is why naive greps "find" them. Do not restore any of them without a maintainer decision.

| Subsystem | Deleted in | What went |
|---|---|---|
| Entire streamrip CLI/TUI package (~13k LOC: `client/`, `media/`, `metadata/`, `rip/cli.py`, `tui/app.py`, `converter.py`, `config.toml`) | `6377bcd` (2026-04-09) | The commit message calls itself "the demolition commit". |
| `backend/services/config_bridge.py` and the last `streamrip` imports in backend/ | `6665460` (2026-04-09) | Both sources now go through the standalone SDKs. |
| Vendored `qobuz-api/` (~3.5k LOC across 30 files) | `1e0c50e` (2026-04-09) | Forward-ported into the submodule; single source of truth. |
| `.github/workflows/poetry-publish.yml` | `b57a0d2` (2026-04-14) | Would have fired a failing PyPI publish on every GitHub Release. |
| Audio-conversion Settings feature (four `conversion_*` config fields, UI section) | `d13c49c` (2026-04-09) | SDK downloads directly; user explicitly opted out of transcoding. |

**Status: SETTLED.** Re-check: `git show --stat --format='%h %s' <sha>` for any of the five SHAs; `ls streamrip` fails on main.

---

## 12. THE STALE BUG LIST — 22 bugs against a deleted codebase

**Symptom:** you find `.ai-project/todos/bug-fixes-review-2026-04-06.md`, an official-looking review listing 22 prioritized bugs (7 P0 crashes, 4 P1, 11 P2), and start planning fixes.

**Root cause of the trap:** every file path in it cites the `streamrip/` CLI/TUI directory tree (`streamrip/client/tidal.py`, `streamrip/metadata/tagger.py`, …) — a codebase deleted wholesale in `6377bcd` (see entry 11). The review predates the web-UI rebuild. It is 100% stale: fixing it means patching files that do not exist. At least one of its themes (SQLite WAL mode) was independently addressed in the new backend. Additionally, `.ai-project/` is gitignored, so the file is invisible in fresh clones — its presence is a local artifact of the maintainer's machine.

**What to do:** ignore the file for work planning. If one of its themes seems worth re-auditing against the NEW backend or SDK code (for example, dedup-DB locking or secrets in debug logs), treat that as a fresh investigation, not a fix of the listed item.

**Status: SETTLED** (fenced off). Re-check: `grep -c "streamrip/" .ai-project/todos/bug-fixes-review-2026-04-06.md` (21 path references as of 2026-07-03; file may be absent in fresh clones).

---

## When NOT to use this skill

- You have a live symptom and need triage steps and discriminating experiments: use **libsync-debugging-playbook**. This skill tells you whether the battle was already fought; that one tells you how to fight a new one.
- You need the current design invariants and known-weak points rather than their history: use **libsync-architecture-contract**.
- You are classifying or gating a change (PRs, required CI checks): use **libsync-change-control**.
- You are working the ongoing Qobuz auth-resilience problem (entry 2 is its prehistory, not its playbook): use **libsync-qobuz-auth-campaign**.
- You need streaming-domain theory (codecs, quality tiers, OAuth flows, app-id/secret model): use **qobuz-tidal-domain-reference**.
- You are releasing, deploying, or deciding what data files are safe to delete: use **libsync-run-and-operate**.

## Provenance and maintenance

Every entry above was verified on 2026-07-03 at v0.0.6. Line numbers drift; commit SHAs do not (absent another reroot). One re-verification command per drift-prone fact:

- Reroot root commit: `git log --reverse --format='%h %s' | head -1` → `f5058e4`
- Replace ref / backup branch (maintainer's clone only): `git for-each-ref refs/replace && git branch --list 'pre-reroot-*'`
- Hidden upstream commit count (maintainer's clone only): `git --no-replace-objects log --oneline eeee774 | wc -l` → 831
- App-id saga commits: `git show --no-patch 6650993 8e1f8e2 cfa4449 1e9db8e e4e0a1f`
- Quality-tier commits: `git show --no-patch 1bcb080 c0e5d96` and `grep -n "24B-192kHz\|FLAC-" CHANGELOG.md`
- Codex cluster: `git show --no-patch bf293f7`; CI submodule lines: `grep -c "submodules: recursive" .github/workflows/pytest.yml`
- Traversal guard location: `grep -n "startswith" backend/main.py` (lines 344-351 as of 2026-07-03)
- PR #13 state: `gh pr view 13 --json state,title` → CLOSED
- Remaining case-sensitive bool sites: `grep -rn '== "True"' backend --include='*.py'` → `backend/api/library.py` lines 111-112 and 161-162 as of 2026-07-03
- Scan progress fix: `git show --no-patch 00e2a19`
- Placeholder-name fix: `git show --no-patch 4d219d2`
- Stranded dev delta: `git log main..origin/dev --oneline | wc -l` → 11; `gh pr view 10 --json state,baseRefName` → MERGED, base `dev`
- SDK re-pin commits: `git show --no-patch 52837cd b57a0d2`; current pin: `git submodule status` → `5e66211`
- Deleted subsystems: `git show --stat 6377bcd 6665460 1e0c50e d13c49c` and `ls streamrip` (must fail)
- Stale bug list: `ls .ai-project/todos/bug-fixes-review-2026-04-06.md` (local-only, gitignored) and `grep -c "streamrip/" <that file>`
