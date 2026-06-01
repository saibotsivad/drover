# Drover Webapp

Optional management UI for [Drover](../README.md). Runs as its own container, talks to an [orchestrator](../orchestrator) over HTTP, and serves an htmx-based PWA. See [`docs/rfc/2026-05-02-management-ui-worker.md`](../docs/rfc/2026-05-02-management-ui-worker.md) for the design rationale and [`docs/planning/webapp-implementation-plan.md`](../docs/planning/webapp-implementation-plan.md) for the phased build plan.

## Stack

- Node 22 LTS, Express 5, `http-proxy-middleware`.
- htmx, vendored at build time — no CDN at runtime.
- Tagged template literals for HTML rendering. The `html` tag in [`src/views/render.js`](src/views/render.js) auto-escapes interpolated values; trusted strings can be passed through `safe()` or composed via nested `html` calls.

## Environment Variables

| Var | Required | Default | Description |
|---|---|---|---|
| `DROVER_ORCHESTRATOR_URL` | yes | — | Base URL of the orchestrator, e.g. `http://orchestrator:8000`. |
| `DROVER_API_KEY` | no | _(unset)_ | Bearer token. When set, the proxy injects `Authorization: Bearer ${DROVER_API_KEY}` on every forwarded request. When unset, no auth header is added. |
| `PORT` | no | `8080` | Port the webapp listens on. |
| `LOG_LEVEL` | no | `info` | One of `debug` / `info` / `warn` / `error`. |

If `DROVER_ORCHESTRATOR_URL` is unset the server logs a structured error and exits.

## Running locally

```sh
npm install
npm run vendor          # copies htmx.min.js into public/vendor/
DROVER_ORCHESTRATOR_URL=http://localhost:8000 npm start
```

`npm run dev` reloads on file changes via `node --watch`.

## Running with Docker

Pull the published image:

```sh
docker run --rm -p 8080:8080 \
  -e DROVER_ORCHESTRATOR_URL=http://orchestrator:8000 \
  -e DROVER_API_KEY=your-token \
  ghcr.io/saibotsivad/drover-webapp:latest
```

Or build locally from this directory:

```sh
docker build -t drover-webapp:local .
docker run --rm -p 8080:8080 \
  -e DROVER_ORCHESTRATOR_URL=http://orchestrator:8000 \
  drover-webapp:local
```

For a full stack with the orchestrator, see [`docker-compose.yml`](../docker-compose.yml) at the repository root:

```sh
docker compose up
# webapp on http://localhost:8080
```

The compose file puts both services on a private user-defined network and only publishes the webapp's port — the orchestrator is reachable from the host only by going through the webapp.

## Vendoring htmx

htmx is pulled in as a `devDependency` and copied into `public/vendor/htmx.min.js` by `scripts/vendor-htmx.mjs` (run via `npm run vendor`). To bump the version, change `htmx.org` in `package.json` and re-run `npm install` and `npm run vendor`.

## Routes

- `GET /health` — webapp-side health check (distinct from the orchestrator's). Returns `{ "healthy": true }`.
- `GET /api/orchestrator/*` — reverse proxy to the orchestrator. The prefix is stripped, the bearer token is injected when configured. Useful for raw curl access through the webapp:

  ```sh
  curl http://localhost:8080/api/orchestrator/health
  curl http://localhost:8080/api/orchestrator/workers
  ```

- `GET /` — rendered home page.
- `GET /views/workers`, `GET /views/workers/:id`, `GET /views/images`, `GET /views/launch` — server-rendered HTML pages.
- `POST /actions/workers`, `POST /actions/workers/:id/stop`, `DELETE /actions/workers/:id` — form/htmx-driven actions that forward to the orchestrator and return HTML fragments.

## Tests

```sh
npm test
```

Uses Node's built-in `node:test` runner; no test-framework dependency.

## Logging

Structured JSON, one line per record. The request middleware logs `method`, `path`, `status`, and `duration_ms` only — request and response bodies are never logged, and `Authorization` headers are never logged. Test coverage in [`test/logger.test.js`](test/logger.test.js) and [`test/proxy.test.js`](test/proxy.test.js) asserts these invariants.

## Trust boundary

The webapp holds the bearer token and adds it to every outbound orchestrator request. **Anyone who can reach the webapp's published port has full Drover access.** For LAN-only homelab use this is fine; for wider exposure, front the webapp with whatever auth you already use (basic auth, an OAuth proxy, an IP allowlist, a VPN, etc.). The RFC has the full discussion.
