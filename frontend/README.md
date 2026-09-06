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

Vite proxies `/api` HTTP requests and `/api/ws` WebSocket connections to
`http://localhost:8080`, keeping the original path. Production clients remain
same-origin; this proxy only applies to the development server.

To verify the split setup without starting a sync or download:

1. Open the Vite URL printed in the terminal (normally `http://localhost:5173`).
2. In browser Network tools, check that `/api/auth/status` returns the backend
   response through the Vite origin.
3. In the WebSocket filter, check that `/api/ws` upgrades with status 101 and
   receives backend events. Keep the connection open to check ongoing traffic.
4. Stop the backend and confirm API requests fail rather than returning the app
   HTML; restart it and reload to confirm HTTP and WebSocket recovery.

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
