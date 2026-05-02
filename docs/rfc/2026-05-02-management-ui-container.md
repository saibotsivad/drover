# Goal

A simple, optional management UI for Drover, shipped as its own Docker image.

# Motivation

We want some kind of UI eventually anyway — for monitoring containers, browsing images, kicking off launches by hand. Shipping it as an optional container means operators who want it just `docker run` it, and operators who don't aren't paying for it.

This is explicitly **not** an enterprise web console. It's a homelab dev tool, in keeping with the rest of Drover.

# Proposal

A new top-level folder `/webapp` containing a Dockerfile that builds an image with:

1. A static PWA built on htmx, talking to a relative `/api/orchestrator/*` path.
2. A Node.js server in front of it that:
   - Serves the PWA's static files (including htmx itself, served from the image — no CDN).
   - Forwards `/api/orchestrator/*` to the orchestrator over plain HTTP, stripping the prefix.
   - Handles WebSocket upgrades for streaming endpoints once they land.
   - Injects the bearer token on outbound requests.
   - Exposes its own `/health` distinct from the orchestrator's.

The operator points the UI container at an orchestrator URL (env var) and supplies a bearer token (env var). The PWA itself never sees the token.

The image publishes to `ghcr.io/saibotsivad/drover-webapp`, alongside the orchestrator's `ghcr.io/saibotsivad/drover`.

# Decisions

## Same-origin reverse proxy

The UI container serves both the PWA and a proxy to the orchestrator. The browser sees one origin: the UI container's published port.

Benefits:

- **No CORS work on the orchestrator.** The orchestrator stays a same-origin service to its proxy.
- **WebSockets just work.** Browser → UI proxy → orchestrator over a plain HTTP upgrade. No auth-header-in-WS problem, no token-in-query-string workaround.
- **Invisible auth in the PWA.** The PWA hits `/api/orchestrator/*`, the proxy adds the bearer token. No UX for entering tokens, no localStorage handling.

## API paths are namespaced under `/api/<service>/*`

The proxy forwards `/api/orchestrator/*` (with the prefix stripped) to the orchestrator. Future server-side services (e.g. a builder layer) would land under their own `/api/<service>/*` namespace and be routed by the same proxy.

Benefits:

- **Room to grow.** New services don't collide with the orchestrator's path space.
- **Clear routing.** It's obvious from any URL whether a request crosses the proxy and to which backend.

## The UI container holds the bearer token

The operator passes `DROVER_API_KEY` (or similar) to the UI container as an env var. The proxy injects it into every forwarded request. The PWA has no auth UX at all.

Benefits:

- **Simple operator setup.** One env var, no per-user token management.
- **No token in the browser.** Nothing in localStorage, nothing in the page source.

Consequence: **the trust boundary is the UI container's published port.** Anyone who can reach it has full Drover access. For a LAN-only homelab this is fine; for any wider exposure, operators front the UI with whatever auth they already use elsewhere (basic auth, an OAuth proxy, an IP allowlist, a VPN, etc.).

## Orchestrator URL is configurable, no shared-network requirement

The UI container takes `DROVER_ORCHESTRATOR_URL=http://orchestrator:8000` (or similar) and resolves it however the operator's environment resolves it — Docker DNS on a shared network, an IP, a hostname over a tailnet, whatever.

Benefits:

- **Same-host setup is trivial.** Join both containers to a Docker network, point the UI at `orchestrator:8000`. A `docker-compose.yml` in the repo will demonstrate this.
- **Remote orchestrators work too.** The UI can run on a different host pointing at a Drover host elsewhere on the LAN/tailnet — just a different URL.
- **No coupling between containers.** Operators who don't want a shared network don't need one.

## Orchestrator auth stays as it is

We keep `DROVER_API_KEY` on the orchestrator (optional, as today). Running the UI doesn't change anything about how the orchestrator authenticates.

Benefits:

- **Defense in depth.** If the orchestrator port is ever published directly for CI / scripts / curl, it's still gated.
- **Independent components.** Removing the UI doesn't suddenly leave an unauthed orchestrator running.

## Proxy implementation: Node.js

The proxy is a small Node.js server, not a config-only proxy like Caddy or nginx.

Benefits:

- **Room to grow.** There are plans for additional server-side functionality over time; a real runtime is more flexible than a config-only proxy.
- **Single language across server and front end.** If the PWA ever grows build tooling, it's the same toolchain.

## PWA stack: htmx, self-hosted

The PWA is built on htmx for reactivity. Htmx's JS files are downloaded into the image at build time and served by the Node.js server alongside the rest of the static assets.

Benefits:

- **Light footprint.** Htmx is small and we avoid a heavy build step.
- **Enough reactivity to be pleasant** without committing to a full framework.
- **No CDN dependency.** The image works on isolated networks; nothing the PWA needs comes from outside the container.

## UI container exposes its own `/health`

Distinct from the orchestrator's `/health` (reachable at `/api/orchestrator/health` once proxied). Lets monitoring distinguish "UI container is up" from "orchestrator is reachable".

## Request logging policy

The proxy logs every request at `INFO` level: method, path, status, and duration.

It explicitly does **not** log:

- Request or response bodies. `POST /containers` request bodies can contain `env` dicts with secrets (e.g. `DROVER_AUTO_GIT_TOKEN`), which must never land in container logs.
- The `Authorization` header injected by the proxy, or any other header that could carry credentials.

Benefits:

- **Useful visibility by default.** Operators can see traffic flowing without flipping a flag.
- **No accidental secret disclosure.** Secrets passed through the API never appear in logs.

# Initial UI Scope

Minimum-viable, first cut:

- List containers, with status.
- View a container's metadata and recent logs.
- Stop / destroy a container.
- List `drover/*` images.
- Launch a container from an image (form-driven `POST /containers`).

Explicitly out of scope for v1:

- Live log/stdout streaming (depends on the WebSocket work in @docs/planning/websocket-streaming-plan.md).
- Kicking off builds (depends on the builder layer in @THOUGHTS.md).
- Multi-orchestrator support (single UI managing several Drover hosts).
- A PWA-facing config endpoint on the UI container. If a real need surfaces later, we'll add it then.

# Related

- @docs/planning/websocket-streaming-plan.md — streaming work that the UI will eventually consume.
- @THOUGHTS.md — the builder layer; a future UI feature would expose builds.
