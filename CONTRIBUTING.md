# Contributing to Libsync

Thanks for your interest in contributing. Libsync is a self-hosted web UI for managing Qobuz and Tidal libraries — see the [README](README.md) for what it does.

## Branch model (Gitflow)

```
feature/* ──▶ dev ──▶ main ──▶ tag (v0.0.x)
```

- **`main`** — stable. Every commit is a release. Required: PR + all status checks + linear history. No direct pushes, no force-pushes.
- **`dev`** — integration. New work merges here first. Required: PR + all status checks. Allows merge commits.
- **`feature/<short-name>`** — your work. Branch off `dev`, PR back into `dev`.
- **Releases** are dev → main PRs followed by an annotated tag (`vX.Y.Z`). The tag push triggers a Docker image build with semver tags.

## Local development

```bash
git clone --recursive git@github.com:arthursoares/libsync.git
cd libsync
make deps                   # init submodule + install both SDKs
make dev                    # build frontend + start backend at :8080
```

Run tests:

```bash
make test                   # unit tests, ~1.5s
poetry run pytest tests/test_specific.py -v   # one file
```

Lint:

```bash
uvx ruff check               # check (CI-equivalent)
uvx ruff format --check      # format check (CI-equivalent)
uvx ruff check --fix         # auto-fix
uvx ruff format              # apply formatting
```

(Local poetry-pinned `ruff = "^0.1"` is older than CI's `chartboost/ruff-action@v1`. Use `uvx ruff` to match CI.)

## Submitting a PR

1. **Branch off `dev`** (not main). Name it `feature/<concise-description>` or `fix/<concise-description>`.
2. **Make your changes.** Keep diffs focused — one logical concern per PR. Fixing two unrelated things? Two PRs.
3. **Tests.** Add tests for new behavior; the suite must stay green.
4. **Lint.** `uvx ruff check && uvx ruff format --check` should pass before pushing.
5. **Commit messages.** Use [Conventional Commits](https://www.conventionalcommits.org/) prefixes — `feat:`, `fix:`, `chore:`, `ci:`, `docs:`, `test:`, `refactor:`. Keep the subject ≤ 70 chars; put detail in the body. Reference issues with `Fixes #N`.
6. **PR description.** Use the provided template — summary + test plan + linked issues.
7. **CI must pass.** Required checks: `backend-tests`, `frontend-build`, `docker-publish`, `ruff`. Non-required (informational): `e2e-tests` (only runs on `dev` pushes with the `QOBUZ_TOKEN` secret).
8. **Merge.** After approval + green CI, merge via the GitHub UI. Branch is auto-deleted on merge.

## Code style

- **Python**: ruff-enforced (`E4 E7 E9 F I ASYNC N RUF ERA001`). 4-space indent. Type hints encouraged but not required outside public APIs.
- **Frontend**: Svelte + TypeScript. Follow the design system in `frontend/src/lib/design-system/tokens.css` — solid shadows, `border-radius: 0`, Atkinson Hyperlegible font, dark-mode default.
- **Comments**: explain *why*, not *what*. Don't comment obvious code. Don't reference the current task or fix in comments — that belongs in the PR body.

## Architecture

Read `CLAUDE.md` for the high-level architecture: backend (`backend/`) is FastAPI + SQLite, frontend (`frontend/`) is SvelteKit, both Qobuz and Tidal go through the standalone [`qobuz_tidal_api_client`](https://github.com/arthursoares/qobuz_tidal_api_client) SDK consumed as a git submodule.

## Updating the SDK submodule

```bash
cd sdks/qobuz_api_client
git fetch && git checkout <new-sha>
cd ../..
git add sdks/qobuz_api_client
git commit -m "chore: bump qobuz_tidal_api_client to <new-sha>"
```

Open as a normal PR — CI verifies the new SDK version still works.

## Reporting bugs / suggesting features

Use the GitHub Issues templates:
- **Bug report** — describe what you saw vs. expected, plus repro steps and env.
- **Feature request** — describe the problem you're solving, the proposed approach, and any alternatives you considered.

For **security issues** see [SECURITY.md](SECURITY.md) — don't open a public issue.

## Code of conduct

Be kind. Disagreements about code are fine; disagreements about people are not. We follow common-sense norms — no harassment, no discrimination, no bad-faith engagement. Repeated violations result in being blocked.

## License

By contributing, you agree your work will be licensed under [GPL-3.0-only](LICENSE) (same as the project).
