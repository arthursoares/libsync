# Frontend

SvelteKit frontend for the streamrip web UI.

The frontend is built as a static app and served by the FastAPI backend in production. In local development you can run it separately with Vite and point it at a running backend.

## Commands

From `frontend/`:

```bash
npm install
npm run dev
npm run build
npm run check
```

From the repo root, the lightweight frontend logic tests can be run with:

```bash
node --test frontend/tests/*.test.js
```

## Local development

Typical split setup:

1. Start the backend from the repo root:

   ```bash
   make dev-backend
   ```

2. In another shell, start the frontend:

   ```bash
   cd frontend
   npm run dev
   ```

The Vite dev server is for frontend iteration only. Production builds are emitted to `frontend/build` and copied into `backend/static/` by the root `Makefile`.

## Structure

```text
frontend/
├── src/routes/              top-level pages (library, search, playlists, downloads, sync, settings)
├── src/lib/components/      reusable UI pieces
├── src/lib/stores/          shared reactive state (library, downloads, websocket, toast)
├── src/lib/api/             API client and error helpers
├── src/lib/design-system/   tokens and shared visual primitives
└── tests/                   small frontend logic tests run with node:test
```

## Current testing scope

- `npm run build` verifies the app compiles for production
- `npm run check` runs `svelte-check`
- `node --test frontend/tests/*.test.js` covers small shared logic helpers

There is currently no browser e2e harness wired into the frontend package.
