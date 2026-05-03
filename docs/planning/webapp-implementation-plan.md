# Webapp Implementation Plan

> Implementation plan for the optional Drover management webapp, per `docs/rfc/2026-05-02-management-ui-container.md`.

---

## Overview

A single Node.js container with three responsibilities:

1. **Static files** — serves the htmx-based PWA from disk (htmx itself is vendored at build time, no CDN).
2. **BFF (HTML fragments)** — server-rendered routes that fetch from the orchestrator and return HTML for htmx to swap into the page.
3. **Reverse proxy** — `/api/orchestrator/*` is forwarded to the orchestrator with the bearer token injected.

The BFF routes call the orchestrator directly (server-side `fetch` to `DROVER_ORCHESTRATOR_URL`), not via the proxy. The proxy exists for any client-side JS that wants raw orchestrator JSON and for operators who want to curl the orchestrator through the webapp.

---

## Stack

| Choice | Notes |
|---|---|
| Node 22 LTS | Pinned in `engines` |
| Express 5 | Boring HTTP framework |
| `http-proxy-middleware` | Mature, well-supported |
| EJS or tagged template literals | Engineer's call; both keep deps small |
| `node:test` | Built-in test runner; no dev deps |
| Vendored `htmx.min.js` | Pinned version, downloaded at image build time |

---

## File Layout

```
webapp/
  Dockerfile
  .dockerignore
  package.json
  package-lock.json
  README.md
  src/
    server.js          # entry: env loading, route wiring, listen
    proxy.js           # /api/orchestrator/* proxy
    logger.js          # request log middleware with redaction
    orchestrator.js    # small fetch wrapper used by BFF routes
    routes/
      health.js
      views.js         # GET HTML fragments for htmx
      actions.js       # POST/DELETE handlers that return HTML fragments
    views/
      layout.html
      partials/
        containers-list.html
        container-detail.html
        images-list.html
        launch-form.html
  public/
    index.html
    css/styles.css
    vendor/
      htmx.min.js
  test/
    proxy.test.js
    logger.test.js
    routes.test.js
```

---

## Environment Variables

| Var | Required | Default | Description |
|---|---|---|---|
| `DROVER_ORCHESTRATOR_URL` | yes | — | e.g. `http://orchestrator:8000` |
| `DROVER_API_KEY` | no | _(unset)_ | Plain-text bearer token. If unset, no `Authorization` header is added — matches the orchestrator's optional-auth model. |
| `PORT` | no | `8080` | Port the webapp listens on |
| `LOG_LEVEL` | no | `info` | `debug`/`info`/`warn`/`error` |

If `DROVER_ORCHESTRATOR_URL` is unset the server logs an error and exits. Other config issues warn and continue.

---

## Phases

### Phase 1: Server Scaffolding

**Goal:** Express app with `/health`, static asset serving, env loading, and a redaction-aware logger.

- [ ] Create `/webapp` with `package.json` (Node engine pinned, npm scripts for `start`, `test`, `dev`)
- [ ] Add Express 5 and `http-proxy-middleware` as runtime deps; no other runtime deps unless justified
- [ ] `src/server.js`: bootstraps the app, validates env, mounts middleware/routes, listens on `PORT`
- [ ] `src/logger.js`: exports `info/warn/error` and a request-logging middleware. Logs method, path, status, duration. **Never logs request/response bodies or `Authorization` headers.**
- [ ] `routes/health.js`: `GET /health` returns `{ healthy: true }` (UI-side health, distinct from orchestrator's)
- [ ] Static middleware serves `/public` at the root
- [ ] Tests: env validation, logger redaction (assert that bodies and `Authorization` never appear in log output)

### Phase 2: Reverse Proxy

**Goal:** `/api/orchestrator/*` reaches the orchestrator with the bearer token attached.

- [ ] Mount `http-proxy-middleware` at `/api/orchestrator` with `pathRewrite: { '^/api/orchestrator': '' }` and `target: DROVER_ORCHESTRATOR_URL`
- [ ] Inject `Authorization: Bearer ${DROVER_API_KEY}` via `onProxyReq` only when `DROVER_API_KEY` is set
- [ ] Route proxied traffic through the redaction-aware logger
- [ ] Tests: prefix stripping, header injection (header present when key set, absent when not), redaction holds for proxied paths

### Phase 3: htmx Shell

**Goal:** A navigable PWA shell with htmx vendored, a base layout, and stubbed BFF routes.

- [ ] Add a build step (npm script) that downloads a pinned `htmx.min.js` into `public/vendor/`. Hash-pinned if the source supports it.
- [ ] Pick rendering approach (EJS or tagged template literals); document choice in `webapp/README.md`
- [ ] `views/layout.html`: header, nav (Containers, Images), content slot, includes `htmx.min.js` from `/vendor/`
- [ ] Hand-rolled CSS — minimal, no framework, no build pipeline
- [ ] `routes/views.js` mounted at `/views/*` with stub responses ("coming soon" fragments)
- [ ] `src/orchestrator.js`: thin `fetch` wrapper (`getJson`, `postJson`, `del`) using env URL + token; surfaces non-2xx as typed errors

### Phase 4: PWA Features

**Goal:** The minimum-viable feature set from the RFC, all server-rendered HTML fragments consumed by htmx.

- [ ] **Container list** — `GET /views/containers`: table of containers with status, periodic refresh via `hx-trigger="every 5s"`
- [ ] **Container detail** — `GET /views/containers/:id`: metadata + recent logs (logs fetched from orchestrator's existing endpoint)
- [ ] **Image list** — `GET /views/images`: lists `drover/*` images
- [ ] **Launch form** — `GET /views/launch`: form for image, label, env, timeout
- [ ] **Launch action** — `POST /actions/containers`: forwards to orchestrator, then redirects to `/views/containers/:id` for the new container (via `HX-Redirect` so htmx navigates the browser)
- [ ] **Stop action** — `POST /actions/containers/:id/stop`: returns updated row
- [ ] **Destroy action** — `DELETE /actions/containers/:id`: returns empty fragment so the row is removed
- [ ] Error-state fragments (orchestrator unreachable, 401, 404) rendered consistently
- [ ] Tests: each route exercised against a mocked `orchestrator.js`; happy path and one error path each

### Phase 5: Packaging & Publishing

**Goal:** The image builds, runs, and gets published to GHCR.

- [ ] `webapp/Dockerfile`: `node:22-slim` base, non-root user, `npm ci --omit=dev`, `CMD ["node", "src/server.js"]`
- [ ] `webapp/.dockerignore`: excludes `test/`, `node_modules/`, etc.
- [ ] `docker-compose.yml` at repo root: orchestrator + webapp on a shared user-defined network, with the only exposed port being the webapp's
- [ ] `.github/workflows/publish-webapp.yml`: mirrors `publish.yml` patterns, publishes to `ghcr.io/saibotsivad/drover-webapp` on tag push
- [ ] CI smoke test: build the image, run it pointing at a mock orchestrator, hit `/health`
- [ ] `webapp/README.md`: env vars, sample `docker run`, sample compose snippet, curl example through the proxy, link back to the RFC

---

## Out of Scope

Captured here so the engineer doesn't drift; full rationale lives in the RFC.

- Live log/stdout streaming.
- Build orchestration UI.
- Multi-orchestrator support.
- A PWA-facing config endpoint.
- TLS termination — operators front the webapp with their own reverse proxy if they want HTTPS.
