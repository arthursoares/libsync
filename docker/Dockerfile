# Stage 1: Build frontend
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python backend
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

# Fixed-uid non-root user (installed while still root). The uid/gid (1000)
# is not configurable at runtime without an entrypoint script, so it must
# match ownership of any host-mounted ./data and ./music directories —
# see README.md and the docker-compose files for the chown one-liner.
RUN groupadd --gid 1000 libsync \
 && useradd --uid 1000 --gid libsync --create-home --shell /usr/sbin/nologin libsync

WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
COPY backend/ ./backend/
# Copy the full submodule checkout so both the qobuz and tidal Python
# packages are installable. The submodule must be initialized on the host
# (`git submodule update --init --recursive`) before running `docker build`.
COPY sdks/qobuz_api_client/clients/python/ ./sdks/qobuz_api_client_py/
RUN pip install --no-cache-dir poetry \
 && poetry config virtualenvs.create false \
 && poetry install --only main --no-interaction \
 && pip install --no-cache-dir \
      websockets \
      ./sdks/qobuz_api_client_py \
      ./sdks/qobuz_api_client_py/tidal
COPY --from=frontend /app/frontend/build ./backend/static/

# Default mount points, pre-created and owned by the non-root user so an
# empty named Docker volume (which inherits the image directory's
# ownership on first creation) is writable out of the box. This has no
# effect on host bind mounts — those keep the host directory's existing
# ownership and must be chowned to uid 1000 separately.
RUN mkdir -p /data /music && chown -R libsync:libsync /app /data /music

USER libsync
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).status == 200 else 1)"
CMD ["uvicorn", "backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
