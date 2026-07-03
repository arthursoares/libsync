---
name: libsync-change-control
description: Use when merging, opening a PR, or classifying a change in the libsync repo — deciding if a change needs a spec, plan, or CHANGELOG entry; when a required check (backend-tests, frontend-build, docker-publish, ruff) blocks a merge; when someone proposes renaming "streamrip" identifiers, overwriting qobuz_app_id, rewriting the SPA path guard for CodeQL, vendoring/bypassing the SDK submodule, editing design tokens, or basing work on origin/dev.
---

# libsync change control

How changes get classified, gated, and reviewed in this repo, and which rules are non-negotiable. Every rule below carries its rationale and, where one exists, the historical incident that created it. All facts verified against the repo and GitHub API as of 2026-07-03 (release v0.0.6).

## 1. Merge gating: what GitHub actually enforces on `main`

Branch protection on `main` (verified 2026-07-03 via `gh api repos/arthursoares/libsync/branches/main/protection`):

| Setting | Value | Practical consequence |
|---|---|---|
| Required status checks | `backend-tests`, `frontend-build`, `docker-publish`, `ruff` | All four must be green on the PR head commit before merge. |
| Strict ("up to date") checks | Enabled | Rebase or update your branch onto latest `main` before merging; checks re-run. |
| Pull request required | Yes, with **0** required approvals | You cannot push directly to `main`, but you can merge your own PR once checks pass. |
| Linear history | Required | No merge commits from the PR side; use squash or rebase merges. |
| Conversation resolution | Required | Every review thread on the PR must be resolved before merge. |
| Force pushes / deletions | Blocked | History on `main` is append-only. |
| `enforce_admins` | Disabled | Admin bypass is technically possible. Do not use it or advise it — the enforced path is PR + green checks. |
| CodeQL | **Not** a required check | A CodeQL alert does not block a merge (see rule 3 below for why one alert is deliberately unresolved). |

Facts that surprise people:

- **`docker-publish` is a required merge check.** The job lives in `.github/workflows/pytest.yml` (job `docker-publish`, workflow name "Tests") — not in a docker-named workflow file. Any Dockerfile or Docker-build breakage therefore blocks **all** merges to `main`, even pure-docs PRs.
- **Every same-repo PR pushes a real image to GHCR** tagged `pr-N` (`ghcr.io/arthursoares/libsync:pr-N`). Fork PRs are build-only because their `GITHUB_TOKEN` lacks package write scope (the "Decide whether to push" step in `pytest.yml`). Opening a PR is itself a publish event; do not put secrets in the image.
- **The `ruff` check runs a different ruff than your local one.** CI (`.github/workflows/ruff.yml`) uses `chartboost/ruff-action@v1` twice (`check`, then `format --check`) over the whole repo. Locally, `make lint` runs the pyproject-pinned ruff 0.1.15 over `backend/` only, and it currently **fails on a clean `main` checkout** with 4 `RUF100` unused-noqa errors that CI does not report (verified 2026-07-03). Never run `ruff --fix` with the local 0.1.15 to "clean up" those noqa comments — CI's newer ruff treats `ASYNC240` as a real rule and the fix would break the required `ruff` check. If `make lint` fails on code you did not touch, compare against CI before changing anything.

## 2. Non-negotiables

Each row is a hard rule. Do not trade any of these away in a PR without explicit maintainer sign-off.

| # | Rule | Why | Incident / source |
|---|---|---|---|
| 1 | Do not rename legacy `streamrip` identifiers before v1.0: the Python module, `STREAMRIP_DB_PATH` / `STREAMRIP_DOWNLOADS_PATH` env vars, the `streamrip.db` SQLite filename, the `.streamrip.json` per-album sentinel, the Docker volume name, and `logging.getLogger("streamrip")`. | Existing deployments reference these names; renaming breaks them. The rebrand to Libsync is deliberately user-facing-only. | CLAUDE.md ("rebrand-only ... Full internal rename is queued for v1.0"). |
| 2 | Never unconditionally overwrite the cached `qobuz_app_id` in the config DB. Qobuz tokens are bound to the app that issued them: OAuth tokens to app `304027809`, manually-pasted web-player tokens to `798273057`. | Commit `8e1f8e2` (2026-04-09) forced the OAuth app id on every boot and broke every user with a web-player-scoped token — at the time the only flow where downloads worked. Commit `cfa4449` partially reverted it and set the rule: only cache an app_id when none is stored; never clobber. Read both with `git show`. | Commits `8e1f8e2` → `cfa4449`. |
| 3 | Do not rewrite the SPA path-traversal guard in `backend/main.py` (`serve_frontend`: `os.path.realpath` + `startswith(static_root + os.sep)`) just to appease CodeQL. | The guard is functionally correct; CodeQL's taint tracker raises a false positive on it. A rewrite to satisfy CodeQL was authored as PR #13 and **deliberately closed unmerged** (2026-04-19). CodeQL is not a required check, so the standing alert costs nothing. | PR #13 (`gh pr view 13` → CLOSED, `mergedAt: null`). |
| 4 | Design-system rules are hard constraints, defined in `frontend/src/lib/design-system/tokens.css`: `border-radius: 0` everywhere, solid shadows with 0px blur (all `--shadow-*` tokens are `Npx Npx 0px`), Atkinson Hyperlegible font only, dark-mode default on warm near-black `#1a1918`, and no CSS transitions except 80ms on hover. | The visual identity is a deliberate, documented decision; "softening" it in one component breaks consistency. | CLAUDE.md Design System section; `tokens.css`. |
| 5 | Both sources (Qobuz and Tidal) go through the standalone SDKs in the `sdks/qobuz_api_client` git submodule. Never vendor SDK code into `backend/`, never call the streaming services' HTTP APIs directly from backend code. | The submodule pins an exact upstream commit; Docker builds and dev installs both reference that path. Vendored copies were already extracted once (the in-repo `qobuz-api/` was deleted when the submodule was created) and drift immediately. | CLAUDE.md Architecture Decisions; `.gitmodules` (URL `arthursoares/qobuz_tidal_api_client`; the path keeps the old name deliberately). |
| 6 | On the scan/manual-mark path, route mark/unmark-as-downloaded through `mark_album_downloaded` / `unmark_album_downloaded` in `backend/services/scan.py`. They are the only **sanctioned** writer of the full triple — (a) `albums.download_status`, (b) the per-source dedup DB row, (c) the `.streamrip.json` sentinel — for that path. **Do not add a fourth independent writer.** Note CLAUDE.md's "only place" wording overstates this: three other writers exist and are legitimate — `DownloadService`'s queue-lifecycle status writes in `backend/services/download.py` (queued → downloading → complete/not_downloaded), the legacy `POST /api/downloads/scan` reconcile in `backend/api/downloads.py`, and the SDK downloaders writing dedup rows/sentinels during real downloads. Do not "fix" those by rerouting them through the scan primitives. | Any new ad-hoc code path that flips the triple will desynchronize the three stores (library DB, dedup DB, on-disk sentinel). Both the scan auto-confirm path and the manual "Mark as downloaded" button already route through the primitives. | `libsync-architecture-contract` invariant I1 (narrowed scope); CLAUDE.md (wording known to read wider than reality — see `libsync-docs-and-writing` backlog); `grep -rn update_album_status backend --include='*.py'`. |

## 3. House rules (inferred — confirm with maintainer)

These are observable patterns the docs do not state as rules. Each is labeled inferred; confirm with the maintainer before treating one as binding, but do not violate them casually.

- **`origin/dev` is abandoned — never base work on it** (inferred — confirm with maintainer). PRs #7–#10 show as MERGED on GitHub but merged into `dev`, which was never merged back to `main`. Their content (multi-arch Docker images, HEAD-request handling on the SPA route, CONTRIBUTING.md, SECURITY.md, issue templates, dependabot) is absent from `main` and from every release. Verify: `git log --oneline main..origin/dev` shows 11 stranded commits (as of 2026-07-03); `ls CONTRIBUTING.md SECURITY.md` fails on `main`. Branch from `main` only.
- **Do not test against live Qobuz/Tidal APIs casually** (inferred — confirm with maintainer). The unit suite (`make test`, ~154 tests) needs no credentials by design; live calls risk the maintainer's accounts and rate limits. The CI job that would run live e2e tests targets a file (`tests/test_e2e_download.py`) that no longer exists and only triggers on pushes to the abandoned `dev` branch.
- **Do not touch the SDK submodule's working tree without maintainer say-so** (inferred — confirm with maintainer). `git status` currently shows the submodule with modified content (` m sdks/qobuz_api_client`, as of 2026-07-03) — uncommitted maintainer work may be in flight there. Submodule updates are a deliberate two-step: pull inside the submodule, then commit the pin bump in this repo.
- **Grep hygiene when researching changes** (inferred, low stakes): repo-root greps match full deleted-CLI-era code copies under `.claude/worktrees/agent-*/` — exclude `.claude`, `node_modules`, `.svelte-kit`, and `sdks` unless you mean to include them.

## 4. Classifying a change

Use this decision list before writing code.

1. **New feature or behavior change?** Historically, features got a design spec plus an implementation plan under `docs/superpowers/specs/` and `docs/superpowers/plans/` before implementation (web UI, Qobuz SDK, docker-compose example, and library scan all did; bug fixes went straight to PR). Requiring a spec for your feature is inferred from this pattern — confirm with maintainer. Note: do not use plan-file checkboxes to judge implementation status; shipped plans still show every box unchecked. Also, README's Contributing section asks that you open an issue first for non-trivial changes.
2. **User-visible change?** It needs a CHANGELOG.md entry. Entries are written per release (every tag v0.0.1–v0.0.6 has a section — except v0.0.5.1, which has a tag but no section; do not repeat that gap). CHANGELOG.md is currently the most release-current document; README, CLAUDE.md, and docs/WEB_UI.md all lag it.
3. **Any change at all?** It goes through a PR against `main` and must pass the four required checks (section 1). Before pushing, run `make test` (README Contributing also says `make lint`, but see the ruff version-skew warning in section 1 — a local `make lint` failure on untouched files is expected on current `main`). The full pre-push checklist lives in the `libsync-validation-and-qa` skill.
4. **Releasing?** Release tagging is change control too: an annotated three-part semver tag (`v*`) on `main` triggers the Tests workflow, whose `docker-publish` job pushes semver-tagged images and `latest` to GHCR. All existing tags v0.0.1–v0.0.6 (plus v0.0.5.1) are annotated. Tag mechanics, versioning, and the data-artifact map live in the `libsync-run-and-operate` skill — follow it rather than tagging ad hoc.

## 5. Solo-maintainer reality

The history is messier than the current rules. PR #1 (the entire web-UI rework) and PR #2 (an external contribution) were closed, not merged — yet their commits landed on `main` anyway via direct push (PR #2's commit was preserved with the original author's name and became the v0.0.1 tag target). Several early PRs "merged" into the now-abandoned `dev` branch instead of `main`.

None of that is precedent. The **currently enforced** path — checked against live branch protection on 2026-07-03 — is: branch from `main`, open a PR, get all four required checks green on an up-to-date branch, resolve all conversations, merge with linear history. With 0 required approvals, self-merge after green checks is the normal solo workflow. Do not route around it, and do not advise others to.

## When NOT to use this skill

- **Cutting a release, tagging, deploying, or asking what is safe to delete** → `libsync-run-and-operate`.
- **What tests exist, what counts as evidence, pre-push checklist matching CI** → `libsync-validation-and-qa`.
- **Setting up the dev environment, submodule init, build traps** → `libsync-build-and-env`.
- **Understanding *why* the architecture invariants exist in depth (dedup DBs, quality tiers, facade shape)** → `libsync-architecture-contract`.
- **The full story of past incidents and dead ends** (this skill only carries the incidents that produced rules) → `libsync-failure-archaeology`.
- **Which document is authoritative and how to write docs here** → `libsync-docs-and-writing`.
- **Something is broken and you need triage, not process** → `libsync-debugging-playbook`.

## Provenance and maintenance

Re-verify each fact before relying on it; all were checked 2026-07-03 at v0.0.6.

- Branch protection (required checks, 0 approvals, linear history, conversation resolution, no force pushes): `gh api repos/arthursoares/libsync/branches/main/protection`
- `docker-publish` job location, pr-N image push, `latest`-on-tag flavor: `grep -n "docker-publish\|event=pr\|startsWith(github.ref" .github/workflows/pytest.yml`
- Ruff CI setup (chartboost action, check + format --check): `cat .github/workflows/ruff.yml`
- Local ruff skew (`make lint` failing on clean main, 4× RUF100): `poetry run ruff --version && poetry run ruff check backend/`
- app_id incident and the resulting rule: `git show 8e1f8e2 --stat` and `git show cfa4449 --stat` (read the full messages)
- PR #13 deliberately closed unmerged: `gh pr view 13 --json state,mergedAt`
- SPA guard still the startswith pattern: `grep -n "startswith\|realpath" backend/main.py`
- State-change primitives: `grep -n "def mark_album_downloaded\|def unmark_album_downloaded" backend/services/scan.py`
- Legitimate direct `download_status` writers outside the primitives (rule 6 caveat): `grep -rn update_album_status backend --include='*.py'` (6 sites in `backend/services/download.py`, 1 in `backend/api/downloads.py`, as of 2026-07-03)
- Design tokens (radius 0, 0px-blur shadows, font, #1a1918): `grep -n "border-radius\|shadow-\|Atkinson\|1a1918" frontend/src/lib/design-system/tokens.css`
- `origin/dev` still stranded: `git log --oneline main..origin/dev | wc -l` and `ls CONTRIBUTING.md SECURITY.md`
- PR base-branch history (which PRs merged into dev vs main): `gh pr list --state all --json number,state,baseRefName,title`
- Tags all annotated, v0.0.5.1 missing from CHANGELOG: `git tag -l 'v*' --format='%(refname:short) %(objecttype)'` and `grep -n "^## " CHANGELOG.md`
- Submodule URL vs path, dirty state: `cat .gitmodules && git status --short sdks/`
- Specs/plans that exist under docs/superpowers/: `ls docs/superpowers/specs docs/superpowers/plans`
