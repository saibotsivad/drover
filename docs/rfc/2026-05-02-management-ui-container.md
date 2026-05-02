# goal

a simple, optional management UI for Drover, shipped as its own Docker image.

# motivation

we want some kind of UI eventually anyway — for monitoring containers, browsing images, kicking off launches by hand. shipping it as an optional container means operators who want it just `docker run` it, and operators who don't aren't paying for it.

this is explicitly **not** an enterprise web console. it's a homelab dev tool, in keeping with the rest of Drover.

# proposal

a new top-level folder `/webapp` containing a Dockerfile that builds an image with:

1. a static PWA built on htmx, talking to a relative `/api/*` path
2. a Node.js server in front of it that:
   - serves the PWA's static files (including htmx itself, served from the image — no CDN)
   - forwards `/api/*` to the orchestrator over plain HTTP
   - handles WebSocket upgrades for streaming endpoints once they land
   - injects the bearer token on outbound requests
   - exposes its own `/health` distinct from the orchestrator's

the operator points the UI container at an orchestrator URL (env var) and supplies a bearer token (env var). the PWA itself never sees the token.

# decisions

## same-origin reverse proxy

the UI container serves both the PWA and a proxy to the orchestrator. the browser sees one origin: the UI container's published port.

benefits:

- **no CORS work on the orchestrator.** the orchestrator stays a same-origin service to its proxy.
- **WebSockets just work.** browser → UI proxy → orchestrator over a plain HTTP upgrade. no auth-header-in-WS problem, no token-in-query-string workaround.
- **invisible auth in the PWA.** PWA hits `/api/*`, the proxy adds the bearer token. no UX for entering tokens, no localStorage handling.

## the UI container holds the bearer token

the operator passes `DROVER_API_KEY` (or similar) to the UI container as an env var. the proxy injects it into every forwarded request. the PWA has no auth UX at all.

benefits:

- **simple operator setup.** one env var, no per-user token management.
- **no token in the browser.** nothing in localStorage, nothing in the page source.

consequence: **the trust boundary is the UI container's published port.** anyone who can reach it has full Drover access. for a LAN-only homelab this is fine; for any wider exposure, operators front the UI with whatever auth they already use elsewhere (basic auth, an oauth proxy, an IP allowlist, a VPN, etc.).

## orchestrator URL is configurable, no shared-network requirement

the UI container takes `DROVER_ORCHESTRATOR_URL=http://orchestrator:8000` (or similar) and resolves it however the operator's environment resolves it — Docker DNS on a shared network, an IP, a hostname over a tailnet, whatever.

benefits:

- **same-host setup is trivial.** join both containers to a Docker network, point the UI at `orchestrator:8000`. a `docker-compose.yml` in the repo will demonstrate this.
- **remote orchestrators work too.** UI can run on a different host pointing at a Drover host elsewhere on the LAN/tailnet — just a different URL.
- **no coupling between containers.** operators who don't want a shared network don't need one.

## orchestrator auth stays as it is

we keep `DROVER_API_KEY` on the orchestrator (optional, as today). running the UI doesn't change anything about how the orchestrator authenticates.

benefits:

- **defense in depth.** if the orchestrator port is ever published directly for CI / scripts / curl, it's still gated.
- **independent components.** removing the UI doesn't suddenly leave an unauthed orchestrator running.

## proxy implementation: Node.js

the proxy is a small Node.js server, not a config-only proxy like Caddy or nginx.

benefits:

- **room to grow.** there are plans for additional server-side functionality over time; a real runtime is more flexible than a config-only proxy.
- **single language across server and front end.** if the PWA ever grows build tooling, it's the same toolchain.

## PWA stack: htmx, self-hosted

the PWA is built on htmx for reactivity. htmx's JS files are downloaded into the image at build time and served by the Node.js server alongside the rest of the static assets.

benefits:

- **light footprint.** htmx is small and we avoid a heavy build step.
- **enough reactivity to be pleasant** without committing to a full framework.
- **no CDN dependency.** the image works on isolated networks; nothing the PWA needs comes from outside the container.

## UI container exposes its own `/health`

distinct from `/api/health` (which proxies to the orchestrator). lets monitoring distinguish "UI container is up" from "orchestrator is reachable".

# initial UI scope

minimum-viable, first cut:

- list containers, with status
- view a container's metadata and recent logs
- stop / destroy a container
- list `drover/*` images
- launch a container from an image (form-driven `POST /containers`)

explicitly out of scope for v1:

- live log/stdout streaming (depends on the WebSocket work in @docs/planning/websocket-streaming-plan.md)
- kicking off builds (depends on the builder layer in @THOUGHTS.md)
- multi-orchestrator support (single UI managing several Drover hosts) — deferred

# open questions

## config endpoint for the PWA

the PWA may want to know things like "is auth enabled", "what's the orchestrator's privileged image", etc. options:

- the PWA hits `/api/health` (already exposed) for the bits it needs
- the UI container exposes a `/config` endpoint that returns UI-specific config
- the PWA is fully static and config-free

leaning option 1 for v1.

## proxy logging verbosity

does the UI container log every proxied request (noisy), only errors (loses visibility), or follow whatever the Node.js server does by default? probably "default" with a knob.

## image name

the orchestrator publishes as `ghcr.io/saibotsivad/drover` (per `publish.yml`). do we publish this as `ghcr.io/saibotsivad/drover-webapp` (matches the folder), `drover-ui` (matches what users would call it), or something else?

# related

- @docs/planning/websocket-streaming-plan.md — streaming work that the UI will eventually consume
- @THOUGHTS.md — the builder layer; a future UI feature would expose builds
