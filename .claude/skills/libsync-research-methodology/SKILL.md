---
name: libsync-research-methodology
description: "Use when a libsync hunch, bug theory, or improvement idea needs to become an accepted change; when a proposed fix explains only some of the symptoms (e.g., library loads but downloads 401); when tempted to ship without a spec, plan, or CHANGELOG entry; when designing any experiment that touches Qobuz/Tidal behavior; when a CodeQL alert or Codex review finding suggests a rewrite; or when deciding whether to remove a feature outright."
---

# Libsync research methodology

This skill defines the discipline that turns a hunch into an accepted change in this repository. Every historical claim below was re-verified against the git history and working tree on 2026-07-03 (repo at v0.0.6, commit ba49a77). Where a rule is practice this skill prescribes rather than something the docs state, it is labeled.

## 1. The evidence bar

A conclusion is accepted only when ONE mechanism explains ALL observations — including the negative ones (the things that still worked, the errors that did NOT appear) — and then survives an adversarial refutation pass in which someone (or some agent) explicitly tries to break it.

**Why the bar is this high — the app-id saga (canonical case).** On 2026-04-09, Qobuz downloads returned 401 while the library page loaded fine. Four fixes shipped in one afternoon before the mechanism was pinned in the fifth (`e4e0a1f`); each intermediate fix explained *part* of the symptom table, shipped anyway, and made a different user population worse — `8e1f8e2`'s own commit message admits a previous fix "made it worse". The full mechanism ("tokens are bound to their issuing app; the header is baked at session construction; only some endpoints validate it; each app id needs its matching signing secret") had to explain both the failures AND the things that kept working before the fix stuck. Full commit-by-commit chronicle: `libsync-failure-archaeology` entry 2.

**Checklist before you claim a mechanism:**

- [ ] Write the full symptom table: what fails, what still works, exact error strings, which user/token population is affected.
- [ ] State one mechanism. Check it against every row, especially the "still works" rows.
- [ ] Run an adversarial refutation pass: assign a reviewer or agent whose explicit job is to break the conclusion, not to polish it. (This repo already wires external adversarial review in: Codex is configured to auto-review every PR — visible in PR comment banners such as on PR #13. Assigning a *dedicated* refutation pass before the PR is this skill's prescription — inferred, confirm with maintainer.)
- [ ] If the refutation pass finds an observation the mechanism cannot explain, the conclusion is dead. Start over; do not patch the story.

**Adversarial review killing a plausible change — PR #13.** PR #13 (`fix(main): harden static-serve path check`, closed unmerged 2026-04-19) rewrote the SPA path-traversal guard purely to appease CodeQL's taint tracker. The existing `startswith` guard was functionally correct (the `../secret.txt` tests pass), and main deliberately kept it — the guard is still in `backend/main.py` (`serve_frontend`) today. Verify with `gh pr view 13 --json state` (state: CLOSED) and `grep -n startswith backend/main.py`. The interpretation "scrutiny correctly killed a plausible-but-unneeded change" is supported by the outcome; the closure rationale itself is not recorded in the PR comments (inferred — confirm with maintainer). Treat it as the precedent: "a scanner complains" is not evidence of a defect, and a change that only silences a tool must clear the same bar as any other change.

## 2. Hypothesis predicts numbers before running

Write the expected observation down BEFORE you run the experiment. Concretely, for this repo that means predictions like:

- Which endpoints will 401 and which will 200 (the app-id saga split on exactly this: `favorites/*` vs `album/*`).
- How many WebSocket events a download of an N-track album should emit (the SDK callbacks `on_track_start` / `on_track_progress` / `on_track_complete` are wired in `backend/services/download.py`, `_download_album`).
- What match rate the scan matcher should produce on a known folder set (the scan test files `tests/test_scan_matcher.py`, `tests/test_scan_normalize.py` encode exactly such expectations).

Rules:

1. Record the prediction (a number, a status code, a count) in the plan, issue, or scratch note before the run.
2. Run the experiment. Compare.
3. If you adjust the prediction after seeing the data, that is a NEW experiment with a new prediction. Post-hoc "well, that's roughly what I expected" counts for nothing.
4. A result nobody can re-run does not count. Record the exact command and the expected output next to the claim (see the Provenance section of this file for the format).

## 3. The idea lifecycle as this repo actually practices it

Every stage below is backed by an artifact you can inspect (all verified 2026-07-03).

| Stage | Artifact | Verified example |
|---|---|---|
| Hunch → validated design | Dated spec in `docs/superpowers/specs/` with validation state recorded IN the spec | `docs/superpowers/specs/2026-04-08-qobuz-library-sdk-design.md` line 3: `> **Status:** VALIDATED (from Proxyman captures 2026-04-08)` |
| Design → executable plan | TDD-style plan in `docs/superpowers/plans/` (checkbox tasks, failing-test-first steps) | `docs/superpowers/plans/2026-04-18-library-scan-fuzzy-match-plan.md` |
| Plan → merged code | PR to main through the four required status checks: `backend-tests`, `frontend-build`, `docker-publish`, `ruff` (branch protection, verified via GitHub API 2026-07-03) | PR #11 (fuzzy scan), PR #15 (v0.0.6) |
| Merged → released | CHANGELOG.md entry per release | `CHANGELOG.md` v0.0.1–v0.0.6 |
| Behavior change → guarded rollout | New config key defaulting to old behavior | `scan_sentinel_write_enabled` (default `True` in `backend/models/schemas.py`, allowlisted in `backend/api/config.py`, documented in CHANGELOG v0.0.3 "Configuration") |
| Retirement | Feature DELETED, not commented out, with the removal documented | Audio-conversion settings stripped end-to-end in `d13c49c` (2026-04-09); the entire streamrip CLI/TUI deleted in `6377bcd` (75 files, −13,751 lines), recorded in the v0.0.1 CHANGELOG entry ("The upstream CLI/TUI/media-pipeline is not part of this project") |

Retirement is a first-class outcome. When a feature loses its reason to exist, delete it whole (schema fields, API coercions, UI section, tests — `d13c49c`'s commit message is the template) and record the removal in the next release's CHANGELOG entry, as v0.0.1 did for the CLI. Do not leave dead toggles or commented-out blocks.

**Honest caveat about plans:** the three shipped plans contain 103, 58, and 80 checkbox tasks respectively and NOT ONE is ticked (`grep -c -- '- \[ \]' docs/superpowers/plans/*.md`), even though all the work shipped and released. Plans in this repo record *intent* at design time; they are never updated during execution. The status record is CHANGELOG.md plus the code itself. Never infer implementation status from plan checkboxes.

## 4. Where good ideas historically came from

Verified sources of accepted changes, ranked by track record:

1. **Traffic captures turned into validated specs.** The Qobuz SDK design was validated endpoint-by-endpoint from Proxyman captures (a local HTTPS proxy used to record real Qobuz app traffic) before any code was written — the spec records the validation date and status in its header. This is the strongest pattern in the repo: observe the real system, write down what you saw, mark the spec VALIDATED, then build.
2. **External code review catching real P1s.** Commit `bf293f7` (2026-04-10) fixed four Codex-review findings (2× P1, 2× P2), including CI missing `submodules: recursive` and an auto-sync diff that was always empty because `refresh_library()` ran before `get_diff()`. The v0.0.5.1 tag exists solely for "Codex review fixes for v0.0.5" (tag on `9e04b08`; note v0.0.5.1 has a tag but no CHANGELOG section of its own). Treat external review findings as hypotheses to verify, not orders — see PR #13 above for review pressure correctly resisted, and the sibling skill `superpowers:receiving-code-review` mindset.
3. **External contributors.** PR #2 from leolobato was closed unmerged as a PR, but its commit landed on main as `7c8062d` with authorship preserved — and became the tag target of v0.0.1 itself.
4. **Recurring bug families pointing at structural fixes.** Quality-tier mapping produced at least four distinct shipped bugs: `1bcb080` (2026-04-07, Settings sent Qobuz format IDs 5/6/7/27 where levels 1–4 were expected), `c0e5d96` (2026-04-18, `tidal_quality` never exposed in Settings so Tidal always used the schema default), and two folder-label-vs-actual-codec bugs in the v0.0.5 CHANGELOG "Fixed" section. When the same family bites a third time, stop spot-fixing: write a spec for the structural fix (a single quality-mapping layer) instead of patching the newest symptom.

## 5. Experiment hygiene in this repo

| Rule | How this repo does it |
|---|---|
| Never experiment against live Qobuz/Tidal services without explicit maintainer authorization | Inferred rule — confirm with maintainer. Supporting evidence: the entire unit suite runs credential-free (CLAUDE.md: "no credentials"; verified `make test` → 154 passed), and CI's e2e job is gated on a repository secret (`QOBUZ_TOKEN`) and a dev-branch-only trigger. |
| Use in-memory DBs and mocked SDK clients in tests | Tests open SQLite at `:memory:` (e.g., `tests/test_api_routes.py`, `tests/test_static_serving.py`); 11 of the 22 test files use `MagicMock`/`AsyncMock`/`mocker` for SDK clients. |
| Prefer simulated failures over induced real ones | The download-failure-threshold and cancel behaviors are tested with mocked downloaders (`tests/test_download_failure_threshold.py`, `tests/test_download_service.py`), not by breaking real downloads. |
| Measure, do not eyeball | Use the runnable scripts shipped by the sibling skill `libsync-diagnostics-and-tooling`. |
| Reproducibility is part of the result | A result is only a result if the command AND expected output are recorded so anyone can re-run it. Format: one line, copy-pasteable, with the expected output stated (see Provenance below). |
| Know the measurement traps | Backend `INFO` logging is invisible at runtime (no logging config in the app), but pytest shows logs (`log_cli=true`, `log_level=DEBUG` in `pyproject.toml`) — absence of runtime log lines is not evidence of absence. Repo-root greps hit `.claude/worktrees/agent-*/` copies of deleted CLI-era code — exclude `.claude`, `node_modules`, `.svelte-kit`, `sdks` or you will "find" code that was deleted in 2026-04. `make lint` fails on clean main due to local-vs-CI ruff version skew — never `--fix` your way out of it. |

## 6. From result to adopted change

A verified result becomes a shipped change only through change control:

1. Route the change through the sibling skill `libsync-change-control` (classification, gates, review). Never route around the PR + required-checks process — `backend-tests`, `frontend-build`, `docker-publish`, and `ruff` are required status checks on main with strict up-to-date enforcement and required linear history (verified via `gh api` 2026-07-03).
2. Put the acceptance criteria in the PR description: the symptom table, the mechanism, the prediction-vs-observed numbers, and the exact verification commands. PR #13's body (test plan with pass/fail per command) is the format to imitate even though that PR was rejected on the merits.
3. Behavior changes get a config key defaulting to the old behavior (`scan_sentinel_write_enabled` precedent) so rollout is guarded and reversible.
4. Update the CHANGELOG entry for the next release, including removals.
5. Update this skill library: when a result changes documented behavior, fix the affected skills' "Provenance and maintenance" facts in the same PR or an immediate follow-up. A skill that documents the old behavior is worse than no skill.

## When NOT to use this skill

- Classifying, gating, or reviewing a specific change → `libsync-change-control`.
- Triage of a live symptom (which experiment discriminates between causes) → `libsync-debugging-playbook`.
- Looking up what a past investigation already concluded → `libsync-failure-archaeology` (do not re-litigate settled battles; the app-id saga's full chronicle lives there).
- First-principles analysis recipes with worked examples → `libsync-proof-and-analysis-toolkit`.
- The specific live Qobuz-auth problem → `libsync-qobuz-auth-campaign`.
- Open research problems (all candidate-labeled) → `libsync-research-frontier`.
- What counts as test evidence and the pre-push checklist → `libsync-validation-and-qa`.
- Measurement scripts → `libsync-diagnostics-and-tooling`.

## Provenance and maintenance

Re-verify each fact with the command given; all were last verified 2026-07-03 at v0.0.6 (commit ba49a77).

- App-id saga (condensed here; full chronicle in `libsync-failure-archaeology` entry 2): `git show -s 8e1f8e2 e4e0a1f` (expect the "made it worse" line in `8e1f8e2`, the APP_SECRET resolution in `e4e0a1f`).
- PR #13 closed unmerged: `gh pr view 13 --json state,title` (expect `"state": "CLOSED"`).
- Traversal guard still the `startswith` pattern on main: `grep -n startswith backend/main.py` (expect a hit inside `serve_frontend`).
- Spec validation-status pattern: `head -5 docs/superpowers/specs/2026-04-08-qobuz-library-sdk-design.md` (expect `> **Status:** VALIDATED (from Proxyman captures 2026-04-08)`).
- Plan checkboxes all unticked: `grep -c -- '- \[ \]' docs/superpowers/plans/*.md` (expect 103 / 58 / 80) and `grep -c -- '- \[x\]' docs/superpowers/plans/*.md` (expect 0 / 0 / 0).
- Required merge checks: `gh api repos/arthursoares/libsync/branches/main/protection --jq '.required_status_checks.contexts'` (expect `["backend-tests","frontend-build","docker-publish","ruff"]`).
- Guarded-rollout precedent: `grep -rn scan_sentinel_write_enabled backend/models/schemas.py backend/api/config.py` (expect default `True` and the allowlist entry).
- Retirement precedents: `git show -s --stat d13c49c | head -25` and `git show -s --stat 6377bcd | tail -3` (expect the conversion-strip message and `75 files changed, ... 13751 deletions`).
- Codex-review fix cluster and patch tag: `git show -s bf293f7 | head -8` and `git tag -l v0.0.5.1 --format='%(contents:subject)'` (expect "Codex review fixes for v0.0.5").
- External-contributor v0.0.1 target: `git show -s --format='%an %ae' v0.0.1^{commit}` (expect `Leonardo Lobato leo@lobato.org`).
- Quality-tier bug family: `git show -s --format='%h %ad %s' --date=short 1bcb080 c0e5d96` and the v0.0.5 "Fixed" section of CHANGELOG.md.
- Test suite size and speed: `make test` (expect `154 passed` in a few seconds, no credentials).
- Test isolation patterns: `grep -rln ':memory:' tests/ | head` (expect multiple files) and `grep -rln 'AsyncMock\|MagicMock\|mocker\.' tests/ | wc -l` (expect ~11).
