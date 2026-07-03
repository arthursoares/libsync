---
name: libsync-external-positioning
description: Use when writing anything public about libsync — README or website copy, release notes, feature claims, screenshots, demo videos, blog or social posts; when handling .har captures, tokens, or /api/config output that could leave the machine; when asked about the GPL license, the nathom/streamrip fork lineage, the 2026-04-14 history reroot, or the FUNDING.yml sponsor button; when an external user wants to file an issue or contribute; or when deciding whether a capability may be claimed.
---

# Libsync External Positioning

This skill governs what may be claimed, shipped, and shown publicly about libsync; the licensing and fork obligations that bind the project; and the secret-handling rules that must never be broken. All facts below were verified against the repo on 2026-07-03 at v0.0.6 unless noted.

## 1. License and lineage

**License**: GPL-3.0-only. The `LICENSE` file at the repo root is the full GPLv3 text. The README License section states: "GPL-3.0-only, same as the upstream [`nathom/streamrip`](https://github.com/nathom/streamrip) project."

**Fork origin is publicly documented.** README.md states, near the top: "Libsync started life as a fork of [nathom/streamrip](https://github.com/nathom/streamrip) and was rebuilt around a web UI. The original `rip` CLI, the TUI, and the Deezer/SoundCloud source clients are no longer part of this project." The README Acknowledgements section credits nathom, Vitiko98, Sorrow446, DashLt, and the inspiring projects (qobuz-dl, Qo-DL Reborn, Tidal-Media-Downloader). Any public description of libsync must preserve this attribution. Never present libsync as a from-scratch project.

**GPL obligations** (binding on any distribution, including Docker images):

| Obligation | What it means here |
|---|---|
| Source availability | The repo is public on GitHub; keep it that way for any release you distribute (images at `ghcr.io/arthursoares/libsync` count as distribution). |
| License preservation | The `LICENSE` file and the README License section stay intact. Derivative code stays GPL-3.0. |
| No relicensing | Code inherited from streamrip (protocol knowledge, tagging, MQA primitives — now mostly in the SDK) cannot be relicensed to anything non-GPL-compatible. |
| Attribution | Keep the README fork notice and Acknowledgements section. |

**The 2026-04-14 history reroot is presentation, not concealment.** The maintainer rewrote local history so the first own commit (`f5058e4`, "Add browse-library feature") is the visible root, cutting off 831 upstream commits. Verified: the local branch `pre-reroot-backup-20260414-170428` shares no merge-base with `main`, a local `refs/replace/eeee774...` ref hides the old parent, and the rewritten commits carry byte-identical trees (verified: commit `7c8062d` on `main` and the backup tip `e8ad7aa` share tree `4431f46`). The backup branch and replace ref are LOCAL ONLY — a fresh clone from GitHub sees neither. Because the fork lineage remains stated in the README, this does not weaken the attribution story; do not describe the reroot publicly as hiding anything, and do not deny the fork.

**Open item — FUNDING.yml sponsors the wrong person.** `.github/FUNDING.yml` still says `github: [nathom]` — the GitHub Sponsor button on the repo funds the upstream streamrip author, not this maintainer. This is an inherited leftover, not a decision. If you touch public-facing repo config, flag this; fixing it goes through a normal PR (see libsync-change-control).

**Version-string trap.** `pyproject.toml` declares `name = "streamrip"` and `version = "3.0.0"` — both inherited from upstream and meaningless. The real release identity is the git tag (v0.0.1 through v0.0.6, plus v0.0.5.1). Never quote "3.0.0" or the frontend `package.json` "0.0.1" as the product version in public materials.

## 2. Secret-handling discipline (hard rules)

These rules are absolute. Breaking any of them can expose real account credentials or third-party signing secrets.

1. **Never commit or share `captures/` or any `.har` file.** A HAR file (HTTP Archive — a browser/proxy traffic capture) from this project contains real auth tokens. The local `captures/` directory holds live Qobuz traffic captures. `.gitignore` already blocks them with the comment "HAR / Proxyman captures contain plaintext auth material — never commit them" (`captures/` and `*.har` entries). Verified 2026-07-03: no `.har` file has ever been committed on any branch (`git log --all --diff-filter=A -- '*.har' 'captures/*'` is empty). Keep it that way — do not attach HARs to issues, gists, or PRs even redacted.
2. **Never paste real tokens or secrets into issues, docs, commit messages, or skill files.** Refer to secrets by constant name and file path only (e.g. "`APP_SECRET` in `qobuz/auth.py`"), never by value.
3. **`GET /api/config` returns credentials in plaintext.** The `AppConfig` response model (`backend/models/schemas.py`) includes `qobuz_token`, `qobuz_app_secret`, and `tidal_access_token` as plain strings, and the handler in `backend/api/config.py` (`get_config`) applies no masking. Therefore: never share raw API responses, curl transcripts, browser devtools screenshots, or Settings-page screenshots that could show these fields. Redact before publishing anything that touched `/api/config`.
4. **The hardcoded credentials are reverse-engineered third-party secrets.** The SDK submodule hardcodes `APP_ID = "304027809"`, a signing `APP_SECRET` (decoded from the official Qobuz desktop Helper bundle), and an OAuth `PRIVATE_KEY` in `sdks/qobuz_api_client/clients/python/qobuz/auth.py`, plus base64-baked Tidal client IDs (`CLIENT_ID`, `CLIENT_ID_PKCE`) in `.../python/tidal/tidal/auth.py`. The backend's fallback web-player app id `798273057` is in `backend/main.py`. These belong to Qobuz and Tidal, not to this project. In public materials (blog posts, release notes, talks): do not draw attention to them, do not quote their values, and do not publish extraction procedures beyond what the repo already contains.
5. **CI holds a live credential.** The `e2e-tests` job in `.github/workflows/pytest.yml` (dev-branch-push only) uses repository secrets `QOBUZ_TOKEN` / `QOBUZ_USER_ID`. Never echo these in workflow steps, and never propose workflow changes that could leak them into logs or fork-triggered runs.

## 3. Positioning claims — what libsync is, publicly

The README already carries the correct framing; reuse its language rather than inventing new claims:

- Libsync is "a self-hosted web UI for managing and downloading your Qobuz and Tidal music libraries."
- "A **premium Qobuz or Tidal subscription** is required for downloads. This project doesn't bypass DRM or account restrictions — it uses your own credentials against the streaming services' own APIs."
- The README Disclaimer: "This software is for personal use with your own legitimately-purchased streaming subscriptions."

Rules derived from that framing:

| Do say | Do not say |
|---|---|
| "Manages your own streaming library with your own paid credentials" | Anything with piracy framing ("rip anything", "free music", "bypass") |
| "Tidal HiRes Lossless via the PKCE flow, given a HiFi-tier subscription and ffmpeg installed" | "HiRes support" unqualified |
| "Docker image at ghcr.io/arthursoares/libsync" | "Multi-arch / runs on ARM" — main's `docker-publish` job has no `platforms:` line, so images are single-arch (linux/amd64); the multi-arch fix is stranded on `origin/dev` (PR #10), unreleased (as of 2026-07-03, v0.0.6) |

**The HiRes claim needs all three qualifiers.** Per CHANGELOG v0.0.5 (verified): (a) the PKCE OAuth flow — the legacy device-code client is entitlement-capped at 320 kbps AAC regardless of subscription; (b) ffmpeg on PATH — without it, DASH-delivered lossless lands as MP4-with-FLAC in a `.flac`-named file; (c) subscription entitlement — "Tidal subscription tier ≠ OAuth client tier. Two separate caps." A public claim of HiRes support must carry all three.

## 4. What must be proven before claiming

A feature is claimable in public materials when it has: tests covering it, a CHANGELOG.md entry, and manual verification recorded (inferred — confirm with maintainer; the repo documents no formal claimability rule, and CHANGELOG entries do not currently record verification evidence).

Regardless of that standard, the documented **known gaps stay listed** and must not be contradicted by public claims (source: `docs/WEB_UI.md` "Known gaps" section, still accurate as of 2026-07-03):

- Qobuz token refresh is manual — no refresh endpoint exists; credentials are re-captured via the OAuth flow.
- Tidal playlists are not supported — the Playlists page shows a placeholder for Tidal.
- No browser E2E harness — frontend build/check and unit tests exist, but no Playwright/Cypress.

Planned or unproven work (the v1.0 internal rename, multi-arch images, anything on `origin/dev`) is labeled "planned" or "unreleased" in public materials, never presented as shipped.

## 5. Ecosystem awareness

- **Upstream streamrip is alive.** `nathom/streamrip` is public, not archived, last pushed 2026-06-18 (verified via `gh repo view` on 2026-07-03). Do not describe it as dead or abandoned.
- **The SDK repo is the right home for SDK-level issues.** `arthursoares/qobuz_tidal_api_client` is public with issues ENABLED (verified 2026-07-03). Auth-flow, signing, DASH, and downloader bugs belong there; consumed via the `sdks/qobuz_api_client` submodule (path keeps the old name deliberately).
- **libsync has NO issue channel — open item.** GitHub issues are DISABLED on `arthursoares/libsync` (verified: `gh repo view --json hasIssuesEnabled` returns false, and `gh issue list` reports "has disabled issues"). The tracked `.github/ISSUE_TEMPLATE/` files on main are stale upstream leftovers: `bug_report.yml` asks for the removed `rip` CLI command, both templates say "streamrip", and `config.yml` sets `blank_issues_enabled: false`. Worse, the README Contributing section says "open an issue first" — currently impossible. Do not tell external users to file libsync issues; PRs are the only working channel today.
- **CONTRIBUTING.md and SECURITY.md exist only on the abandoned `origin/dev` branch** (PR #8), absent from main (verified: `ls CONTRIBUTING.md SECURITY.md` fails on main). Do not link to them as if published.

## 6. Reproducibility standard for anything published

Any published benchmark, demo, screenshot, or bug report referencing behavior must pin all three of:

1. **Exact release tag** — e.g. `v0.0.6`. Use 3-part semver tags only: the tag-push trigger in `.github/workflows/pytest.yml` publishes versioned GHCR images via semver patterns, and a 4-part tag like `v0.0.5.1` produced no versioned image tag.
2. **Docker image digest** — `docker buildx imagetools inspect ghcr.io/arthursoares/libsync:v0.0.6` (or `docker inspect --format '{{index .RepoDigests 0}}'` on a pulled image). The `:latest` tag is repointed on every tag push and is not a stable reference.
3. **Config used** — the relevant `/api/config` keys and env vars (`STREAMRIP_DB_PATH`, `STREAMRIP_DOWNLOADS_PATH`), with all token/secret fields redacted per section 2.

## When NOT to use this skill

- How a change gets approved, gated, and merged (PRs + required checks `backend-tests`, `frontend-build`, `docker-publish`, `ruff`) → **libsync-change-control**.
- Cutting a release, pushing tags, operating deployments → **libsync-run-and-operate**.
- How Qobuz/Tidal auth, signing, quality tiers, and DASH actually work technically → **qobuz-tidal-domain-reference**.
- Improving Qobuz auth resilience (the live campaign) → **libsync-qobuz-auth-campaign**.
- Config keys, defaults, and precedence → **libsync-config-and-flags**.
- Doc house style and which doc is authoritative → **libsync-docs-and-writing**.
- What counts as test evidence before a PR → **libsync-validation-and-qa**.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it:

- License is GPL-3.0: `head -5 LICENSE`
- README fork wording intact: `grep -n "started life as a fork" README.md`
- README license/disclaimer wording: `grep -n -A2 "## License\|## Disclaimer" README.md`
- FUNDING.yml still sponsors nathom: `cat .github/FUNDING.yml`
- Issues still disabled on libsync: `gh repo view arthursoares/libsync --json hasIssuesEnabled`
- SDK repo public with issues on: `gh repo view arthursoares/qobuz_tidal_api_client --json visibility,hasIssuesEnabled`
- Upstream streamrip status: `gh repo view nathom/streamrip --json isArchived,pushedAt`
- No .har ever committed: `git log --all --oneline --diff-filter=A -- '*.har' 'captures/*'` (expect empty)
- .har/captures still gitignored: `grep -n -i "har\|captures" .gitignore`
- /api/config still returns plaintext tokens: `grep -n -A 12 "class AppConfig" backend/models/schemas.py`
- Hardcoded SDK credentials still present: `grep -n "APP_SECRET\|APP_ID" sdks/qobuz_api_client/clients/python/qobuz/auth.py`
- Docker images still single-arch: `grep -n "platforms" .github/workflows/pytest.yml` (expect no match)
- Known gaps still current: `grep -n -A 6 "## Known gaps" docs/WEB_UI.md`
- CONTRIBUTING/SECURITY still dev-only: `ls CONTRIBUTING.md SECURITY.md; git ls-tree origin/dev --name-only | grep -E "CONTRIBUTING|SECURITY"`
- Stale issue templates still on main: `grep -n "rip url\|blank_issues_enabled" .github/ISSUE_TEMPLATE/*.yml`
- Release tags: `git tag -l`
- pyproject version still inherited noise: `head -3 pyproject.toml`
- Reroot artifacts (local clone only): `git for-each-ref | grep -E "replace|pre-reroot"`
