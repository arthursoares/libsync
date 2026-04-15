# Security Policy

## Supported versions

Only the latest tagged release on `main` is supported. Libsync is pre-1.0 — there are no LTS branches.

| Version | Supported |
|---------|-----------|
| `v0.0.x` (latest) | ✅ |
| Older `v0.0.x` | ❌ — upgrade to current |
| Pre-detach (`streamrip` fork) | ❌ — different project |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use one of these private channels:

1. **GitHub private security advisories** (preferred) — go to https://github.com/arthursoares/libsync/security/advisories/new and submit a draft advisory. This creates a private discussion thread.
2. **Email** — `github@arthursoares.com` with subject prefix `[libsync security]`.

Include in your report:
- Affected version (commit SHA or tag).
- Description of the vulnerability.
- Reproduction steps or proof-of-concept.
- Impact assessment (what an attacker could achieve).
- Suggested fix, if you have one.

## What to expect

- **Acknowledgement**: within 7 days.
- **Triage and fix timeline**: communicated within 14 days. Critical issues are prioritized.
- **Coordinated disclosure**: we'll work with you on a disclosure date. Default is 90 days from the report or release of the fix, whichever is sooner.
- **Credit**: reporters are credited in the release notes unless they prefer to remain anonymous.

## Scope

In scope:
- **Backend** (FastAPI app in `backend/`) — auth, OAuth flows, file serving, SQL handling, file system access.
- **Frontend** (SvelteKit app in `frontend/`) — XSS, CSRF, sensitive data exposure.
- **Docker image** (`ghcr.io/arthursoares/libsync`) — supply chain, image contents.

Out of scope (these belong upstream):
- Qobuz / Tidal API vulnerabilities (report to those vendors).
- Issues in the `qobuz_tidal_api_client` SDK (report at [its repo](https://github.com/arthursoares/qobuz_tidal_api_client/security/advisories)).

## Security-relevant context

- Libsync stores credentials (Qobuz / Tidal OAuth tokens) in a local SQLite database. The DB is **not encrypted at rest** — protect the host filesystem and Docker volume accordingly.
- The web UI has **no built-in authentication**. Bind it to `localhost` or put it behind a reverse proxy with auth (Caddy, Traefik, nginx + basic-auth, Authelia, etc.) before exposing it to a network.
- The static-file route (`backend/main.py`) enforces realpath containment to prevent path traversal — that's a v0.0.2 fix, see the changelog.
