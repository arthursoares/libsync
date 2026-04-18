# Docker Compose Example — Design

## Context

The repo already ships `docker/docker-compose.yml`, but it builds the image from local source. That's useful for contributors, not for someone who just wants to run Libsync.

The recent CI work (`3357c13`) publishes prebuilt images to GHCR on `main`, `dev`, PRs, and tags. A user-facing compose example that pulls from GHCR removes the need to clone the repo or run `docker build` at all.

The README's Quick Start currently shows only `docker run`. Compose is a better default for a self-hosted service people want to leave running.

## Goal

Give a new user a copy-paste compose file that runs Libsync against the published image, with persistent config and a visible on-disk data directory.

## Deliverables

1. **`docker-compose.example.yml`** at the repo root.
2. **"Docker Compose" subsection** in README Quick Start, just after the existing `docker run` block.

Non-goals:

- No changes to `docker/docker-compose.yml` (stays as the build-from-source variant for contributors).
- No changes to the Dockerfile or CI.
- No Traefik/Caddy/reverse-proxy examples — out of scope.

## `docker-compose.example.yml`

```yaml
services:
  libsync:
    image: ghcr.io/arthursoares/libsync:latest
    container_name: libsync
    ports:
      - "8080:8080"
    volumes:
      # Where downloaded music is written. Point this at your library root.
      - ./music:/music
      # Persists config, credentials, library DB, and per-source dedup DBs.
      # Contains streamrip.db, downloads.db, downloads-tidal.db — back this up.
      - ./data:/data
    environment:
      - STREAMRIP_DB_PATH=/data/streamrip.db
    restart: unless-stopped
```

Design notes:

- **Bind mounts, not named volumes.** `./data` sits next to the compose file so the user can see, back up, and move their config without `docker volume` commands. Same for `./music`.
- **Service name `libsync`**, not `streamrip`. User-facing names should reflect the rebrand even though the env var / DB filename keep the legacy names per CLAUDE.md.
- **`:latest` tag.** Per the workflow in `.github/workflows/pytest.yml`, `:latest` is only applied on semver tag pushes (`v*`) — it tracks stable releases, currently `v0.0.2`. Users who want the rolling `main` or `dev` branch builds can swap to `:main` / `:dev`. `:latest` is the right Quick-Start default.
- **`container_name: libsync`** so `docker logs libsync` / `docker exec -it libsync sh` work without looking up the generated name.
- **Keep `STREAMRIP_DB_PATH` explicit.** It's the one env var the app actually reads (backend/main.py:242). Worth showing so users see where config lands.

## README addition

New subsection inserted after the existing `docker run` block in the Quick Start section. Keep it short — point at the example file rather than duplicating it:

````markdown
### Using Docker Compose

A ready-to-use compose file is in `docker-compose.example.yml`. Copy it,
adjust the `./music` path if you want to write elsewhere, and start it:

```bash
curl -O https://raw.githubusercontent.com/arthursoares/libsync/main/docker-compose.example.yml
docker compose -f docker-compose.example.yml up -d
```

Config, credentials, and the library database land in `./data` next to the
compose file — back that directory up to preserve your setup.
````

The `curl` line means users don't need to clone the repo to get going.

## Persistence behavior (for reference)

What lives in `/data` and therefore gets persisted by the `./data:/data` mount:

- `streamrip.db` — Qobuz/Tidal OAuth tokens, settings (download path, quality tier, folder/track templates), library (albums, tracks, playlists), sync history, config key/value table.
- `downloads.db` — Qobuz per-track dedup DB.
- `downloads-tidal.db` — Tidal per-track dedup DB.

All three resolve from `os.path.dirname(STREAMRIP_DB_PATH)` (backend/services/download.py:296-299), so mounting `/data` covers everything. Music files go to `/music` (or wherever the user sets Download Path in Settings).

## Out of scope / future

- `.env` file pattern for overriding the image tag or ports. Can add later if users ask.
- A compose profile that layers in a reverse proxy. Separate doc if needed.
- Renaming the env var / DB filename to drop the `streamrip` prefix. Tracked for v1.0 per CLAUDE.md.
