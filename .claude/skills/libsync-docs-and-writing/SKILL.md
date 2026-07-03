---
name: libsync-docs-and-writing
description: Use when writing or updating any libsync doc — CHANGELOG entries, README, CLAUDE.md, docs/WEB_UI.md, specs/plans under docs/superpowers/, or a new skill; when two docs disagree (e.g. about Tidal auth or endpoints); when a doc references a file that does not exist; when deciding which doc to trust or which doc to update after adding an endpoint or config key; or when asked about the house changelog format, commit-message style, or spec/plan naming convention.
---

# libsync docs and writing

This skill tells you which document is authoritative for which question, what is currently stale, and how to write new documentation that matches the house style. All facts below were verified against the repo on 2026-07-03 (current release v0.0.6).

Definitions used throughout:

- **Doc of record**: the file you should trust and update for a given topic.
- **PKCE**: Proof Key for Code Exchange, the OAuth flow behind the "Connect Tidal (HiRes)" button added in v0.0.5.
- **Change control**: all doc edits go through the normal PR flow with the required CI checks (backend-tests, frontend-build, docker-publish, ruff). Docs are not exempt.

## 1. Authority table

| Doc | Authoritative for | Freshness caveat (as of 2026-07-03, v0.0.6) |
|---|---|---|
| `CHANGELOG.md` | What actually shipped in each release; the most current record. Trust it for anything post-2026-04-19. | Missing a section for the `v0.0.5.1` tag (tag message: "Codex review fixes for v0.0.5"). Everything else through v0.0.6 is current. |
| `CLAUDE.md` | Architecture decisions, project rules, key commands. | Two known defects. (a) Dead reference: the "Cleanup plan: `~/.claude/plans/linear-tickling-aho.md`" line points at a machine-local file that does not exist — do not go looking for it or cite it. (b) Overstatement: it says `mark_album_downloaded`/`unmark_album_downloaded` in `backend/services/scan.py` are "the only place" that updates `albums.download_status`. In fact `backend/services/download.py` and `backend/api/downloads.py` also call `db.update_album_status(...)` directly for the queued/downloading/not_downloaded lifecycle. The primitives are the only place that performs the full mark-as-downloaded triple (status + dedup DB + sentinel), not the only writer of the status column. |
| `README.md` | Setup, prerequisites, first-time flow, contributing rules (run `make test` and `make lint` before a PR; open an issue first for non-trivial changes). | Stale on Tidal auth: "First-time setup" step 3 describes only the device-code flow ("click Connect Tidal to start the device-code OAuth flow"). Since v0.0.5 the Settings UI offers "Connect Tidal (HiRes)" (PKCE, primary) plus "Login with Tidal (legacy, AAC only)" (device-code). Last committed 2026-04-19 (commit 9cdbe8d). |
| `docs/WEB_UI.md` | API/endpoint reference, env var table, SDK-update runbook, known gaps. | One release stale: last committed 2026-04-19 (commit 9cdbe8d), so it predates v0.0.5 entirely. Its API table lacks `POST /api/auth/tidal/pkce-start` and `POST /api/auth/tidal/pkce-complete` (both exist in `backend/api/auth.py`) and never mentions the `tidal_auth_method` config key (exists in `backend/models/schemas.py`, default `"device_code"`). Line 1 is still titled "Streamrip Web UI", and its "SDKs come from a submodule" bullet links `github.com/arthursoares/qobuz_api_client` — the real submodule URL per `.gitmodules` is `https://github.com/arthursoares/qobuz_tidal_api_client.git`. For anything Tidal-auth related, trust `CHANGELOG.md` (v0.0.5 section) plus `backend/api/auth.py`, not WEB_UI.md. |
| `docs/superpowers/specs/*` and `docs/superpowers/plans/*` | Historical design intent only. NOT implementation status. | All three plan files have every checkbox unchecked (103, 58, 80 unchecked; 0 checked) even though the work shipped. The founding spec `specs/2026-04-06-streamrip-web-ui-design.md` contains a "CLI Preserved" section that was abandoned — the CLI was removed entirely. Never infer status from a spec or plan; use `CHANGELOG.md` and the code. |
| `.ai-project/` and `.ai-assistant/` | Nothing. Gitignored AI-tooling template scaffolding (`.gitignore` lines 39–41 as of 2026-07-03). | The only git-tracked file inside is `.ai-project/decisions/refactoring-opportunities.md`. One dated todo file exists locally (`.ai-project/todos/bug-fixes-review-2026-04-06.md`, referenced by the founding spec) but it is invisible to fresh clones. Do not cite these directories as docs of record. |
| `frontend/README.md` | Frontend dev commands (Vite dev server, `npm run check`). | Opening line still says "SvelteKit frontend for the streamrip web UI" — rebrand missed it. |

Resolution rule when docs conflict: code beats `CHANGELOG.md`, `CHANGELOG.md` beats everything committed on or before 2026-04-19 (README, CLAUDE.md, WEB_UI.md), and specs/plans lose to all of the above.

## 2. The staleness backlog

Each item below is executable by a junior engineer through normal change control (branch, PR, all required CI checks green). Verify each claim against the code on the day you fix it — do not trust this list blind.

| # | Status | File | Fix |
|---|---|---|---|
| 1 | open | `docs/WEB_UI.md` | Retitle line 1 from "Streamrip Web UI" to "Libsync Web UI" (match the v0.0.6 UI rebrand). |
| 2 | open | `docs/WEB_UI.md` | Fix BOTH dead submodule links (`arthursoares/qobuz_api_client` → `arthursoares/qobuz_tidal_api_client`): one in the "SDKs come from a submodule" bullet, one in the Testing section's "upstream repo" link. Confirm against `.gitmodules`; note the submodule *path* `sdks/qobuz_api_client` intentionally keeps the old name. |
| 3 | open | `docs/WEB_UI.md` | Add `POST /api/auth/tidal/pkce-start` and `POST /api/auth/tidal/pkce-complete` rows to the "API reference" table. Copy request/response shapes from `backend/api/auth.py` (`tidal_pkce_start`, `tidal_pkce_complete`) and the v0.0.5 CHANGELOG "API additions" table. Also document the `tidal_auth_method` config key from `backend/models/schemas.py`. |
| 4 | open | `docs/WEB_UI.md` + `README.md` | Rewrite the Tidal setup instructions: primary flow is "Connect Tidal (HiRes)" (PKCE, manual redirect-URL paste), device-code is "Login with Tidal (legacy, AAC only)". Ground truth: `frontend/src/routes/settings/+page.svelte` (search for "Connect Tidal") and CHANGELOG v0.0.5. |
| 5 | open | `CLAUDE.md` | Remove or replace the dead "Cleanup plan: `~/.claude/plans/linear-tickling-aho.md`" reference. |
| 6 | open | `CLAUDE.md` | Reword the scan-primitives bullet so "only place" scopes to the mark-as-downloaded triple, not to all writes of `albums.download_status` (see Authority table row for the evidence). |
| 7 | open | `CHANGELOG.md` | Add a `## v0.0.5.1 — 2026-04-27` section between v0.0.6 and v0.0.5. Content source: the annotated tag message (`git tag -n99 v0.0.5.1`) and `git log v0.0.5..v0.0.5.1 --oneline`. |
| 8 | candidate | `frontend/README.md` | Update "streamrip web UI" wording to Libsync. Low stakes; batch with item 1. |
| 9 | candidate | `docs/superpowers/specs/2026-04-06-streamrip-web-ui-design.md` | Add a one-line "historical — see CHANGELOG.md for what actually shipped" banner rather than rewriting the spec. Whether specs should be edited at all after shipping is inferred — confirm with maintainer. |

Do not fold the internal streamrip→libsync rename (Python module, `STREAMRIP_DB_PATH`, `streamrip.db`, `.streamrip.json`, logger name, Docker volume) into any of these doc fixes. CLAUDE.md states that rename is deliberately deferred to v1.0.

## 3. House style

Derived from the existing docs and git history; where a rule is inferred rather than stated, it is labeled.

**Commit messages.** Conventional-commit-ish with scope, observed in `git log`: `feat(frontend): …`, `fix(scan): …`, `fix(download): …`, `chore(sdk): …`, `docs(release): …`, `ci: …`, `test: …`. Not enforced by tooling and not 100% consistent historically (one commit is literally "hello (#5)"), so treat it as strong convention, not a gate (inferred — confirm with maintainer).

**Release commit pattern.** Each release since v0.0.4 is a commit on main titled exactly `docs(release): CHANGELOG entry for vX.Y.Z`, with an annotated git tag on that same commit (verified: v0.0.6 = ba49a77, v0.0.5 = 92afe9a). CHANGELOG's own header states: "Release tags are annotated git tags; each section below mirrors the tag message."

**CHANGELOG format** (verified against the v0.0.5 and v0.0.6 entries):

- Section header: `## vX.Y.Z — YYYY-MM-DD` followed by a one-to-two-sentence prose summary of the release.
- Grouped `###` subsections, used as needed: `Added`, `Changed`, `Fixed`, `SDK`, `Schema`, `API additions`, `Notes`, `Internal`.
- Bullets open with a **bold lead phrase** ending in a period, then explanation: `- **Retry button** on failed/cancelled rows…`.
- New endpoints go in an `### API additions` markdown table with columns `Endpoint | Method | Description` (introduced in v0.0.5).
- Each section ends with `### Commits since vPREV` containing a GitHub compare link, then a `---` separator.

**Spec/plan naming.** Specs: `docs/superpowers/specs/YYYY-MM-DD-<slug>-design.md`. Plans: `docs/superpowers/plans/YYYY-MM-DD-<slug>-plan.md`. Date is the authoring date. Verified against all seven existing files.

**Doc register.** README is the "getting started" page; `docs/WEB_UI.md` is the reference (WEB_UI.md says this about itself in its first paragraph). Keep setup steps in README short and numbered; put tables, endpoint lists, and runbooks in WEB_UI.md.

## 4. Rules for new docs

1. Date-stamp volatile facts: version numbers, endpoint counts, "currently missing X" claims get "(as of YYYY-MM-DD, vX.Y.Z)".
2. Never document a flag, endpoint, or config key without verifying it in code the same day. Endpoints live in `backend/api/`, config keys in `backend/models/schemas.py` (`AppConfig`), env vars in `backend/main.py`.
3. Update the `docs/WEB_UI.md` API reference table in the SAME PR that adds or changes an endpoint. The cautionary tale: v0.0.5 added the two PKCE endpoints and `tidal_auth_method` without touching WEB_UI.md, and the reference has been wrong for every release since (backlog items 3–4 above).
4. If a feature ships, its CHANGELOG entry is written in the release PR using the format in section 3 — the CHANGELOG is the status record; plan checkboxes are not maintained after execution.
5. Do not add references to gitignored paths (`.ai-project/`, `.ai-assistant/`, `.claude/`, `~/.claude/...`) in tracked docs — they break on fresh clones. The dead CLAUDE.md cleanup-plan reference and the founding spec's `.ai-project/todos/...` reference are the existing examples of this failure.
6. Skills in `.claude/skills/` follow this same discipline: repo-relative paths, date-stamped volatile facts, and a "Provenance and maintenance" section listing a re-verification command per drift-prone claim.
7. All doc changes go through the normal PR flow with the required CI checks (backend-tests, frontend-build, docker-publish, ruff). Do not push doc edits directly to main.

## 5. Writing for this audience

The standing audience for repo docs and skills is a zero-context junior/mid engineer or a smaller model. Concretely:

- Imperative runbook voice: "Run X. If you see Y, do Z." Not "one might consider running X".
- Define every jargon term once at first use (PKCE, dedup DB, sentinel, submodule pin).
- Prefer tables and numbered checklists over prose paragraphs.
- Commands must be copy-pasteable as written, from the repo root, with repo-relative paths.
- Complete sentences; no fragments.
- State what is verified versus inferred versus planned. Planned or unproven work stays labeled "open" or "candidate"; never present it as shipped.

## When NOT to use this skill

- To decide how a code change is classified, gated, or reviewed → use **libsync-change-control**.
- For which architecture decisions are load-bearing (versus how they are documented) → use **libsync-architecture-contract**.
- For config keys and env vars themselves (defaults, precedence, traps) → use **libsync-config-and-flags**; this skill only covers how to document them.
- For the release/deploy procedure itself (tagging, GHCR publish) → use **libsync-run-and-operate**; this skill only covers the CHANGELOG text and release-commit message.
- For what counts as test evidence before a PR → use **libsync-validation-and-qa**.
- For public claims, GPL/fork obligations, and secret-handling in public text → use **libsync-external-positioning**.

## Provenance and maintenance

Re-verify each drift-prone fact before relying on it:

- CHANGELOG latest version and missing v0.0.5.1 section: `grep -n "^## " CHANGELOG.md && git tag`
- v0.0.5.1 tag message (source for backlog item 7): `git tag -n99 v0.0.5.1`
- WEB_UI.md stale title: `head -1 docs/WEB_UI.md`
- WEB_UI.md dead submodule link vs real URL: `grep -n "github.com/arthursoares/qobuz" docs/WEB_UI.md; cat .gitmodules`
- PKCE endpoints exist in code but not in WEB_UI.md: `grep -n "pkce" backend/api/auth.py docs/WEB_UI.md`
- `tidal_auth_method` exists in code but not in WEB_UI.md: `grep -rn "tidal_auth_method" backend/models/schemas.py docs/WEB_UI.md`
- README Tidal step still device-code-only: `grep -n "device-code" README.md`
- Current Settings button labels: `grep -n "Connect Tidal\|legacy, AAC" frontend/src/routes/settings/+page.svelte`
- CLAUDE.md dead plan reference and "only place" wording: `grep -n "Cleanup plan\|only place" CLAUDE.md`
- Other writers of `albums.download_status` (evidence for the overstatement): `grep -rn "update_album_status" backend --include="*.py"`
- Doc last-commit dates: `git log -1 --format="%h %ad" --date=short -- README.md CLAUDE.md docs/WEB_UI.md` (run per file)
- Plan checkboxes still unchecked: `grep -c -- "- \[ \]" docs/superpowers/plans/*.md && grep -c -- "- \[x\]" docs/superpowers/plans/*.md`
- Release commit pattern: `git log --oneline --grep="docs(release)"`
- Spec/plan naming: `ls docs/superpowers/specs/ docs/superpowers/plans/`
- Gitignored AI-tooling dirs and the one tracked file: `grep -n "ai-project\|ai-assistant\|claude" .gitignore && git ls-files | grep "^\.ai"`
