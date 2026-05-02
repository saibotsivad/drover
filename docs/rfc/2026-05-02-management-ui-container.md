# goal

a simple, optional management UI for Drover, shipped as its own Docker image

# motivation

we want some kind of UI eventually anyway — for monitoring containers, browsing images, kicking off launches by hand. shipping it as an optional container means operators who want it just `docker run` it, and operators who don't aren't paying for it.

this is explicitly **not** an enterprise web console. it's a homelab dev tool, in keeping with the rest of Drover.

an earlier note in @TODO.md ("Different container auth") sketched a related idea where the orchestrator would have no auth at all and the UI container would be the auth layer over a shared Docker network. this RFC is a different shape — see "alternatives considered" at the bottom.

# proposal

a new top-level folder (e.g. `ui/`) containing a Dockerfile that builds an image with:

1. a static PWA — vanilla, no build step if we can manage it — that talks to a relative `/api/*` path
2. a tiny reverse proxy in front of it (Caddy is the leading candidate) that:
   - serves the PWA's static files
   - forwards `/api/*` to the orchestrator over plain HTTP
   - handles WebSocket upgrades for streaming endpoints once they land
   - injects the bearer token on outbound requests

the operator points the UI container at an orchestrator URL (env var) and supplies a bearer token (env var). the PWA itself never sees the token.

# what's decided

## same-origin reverse proxy, not direct fetch from PWA to orchestrator

the original sketch had the PWA make CORS-enabled fetch calls directly to the orchestrator. switching to a reverse proxy on the UI container kills two birds:

- **no CORS work on the orchestrator.** browser sees one origin (the UI container's published port). orchestrator stays a same-origin service to its proxy.
- **WebSockets just work.** browser → UI proxy → orchestrator over a plain HTTP upgrade. no auth-header-in-WS problem, no token-in-query-string workaround.

the cost is that the UI container is no longer "just a static file server" — it now also runs a proxy. Caddy makes this ~10 lines of config.

## the UI container holds the bearer token, not the PWA

the operator passes `DROVER_API_KEY` (or similar) to the UI container as an env var. the proxy injects it into every forwarded request. the PWA has no auth UX at all.

consequence: **the trust boundary is the UI container's published port.** anyone who can reach it has full Drover access. for a LAN-only homelab this is fine; for any wider exposure, operators put the UI behind their own auth (basic auth, an oauth proxy, an IP allowlist, a VPN, whatever they already use for everything else on the box).

we considered keeping the token in the PWA's localStorage (so the proxy is dumb) but it adds UX without adding security in this topology — the proxy could read either way.

## orchestrator URL is configurable, no shared-network requirement

an earlier draft of this idea required the UI and orchestrator to be on the same Docker network and resolve `orchestrator:8000` by Docker DNS. that works but it locks you to a single host.

instead, the UI container takes an env var like `DROVER_ORCHESTRATOR_URL=http://orchestrator:8000` and resolves it however the operator's environment resolves it — Docker DNS on a shared network, an IP, a hostname over a tailnet, whatever. same-host shared-network is the easy default; remote is just a different URL.

a `docker-compose.yml` in the repo will demonstrate the same-host setup.

## orchestrator auth stays as it is

we keep `DROVER_API_KEY` on the orchestrator (optional, as today). running the UI doesn't change anything about how the orchestrator authenticates.

this is defense in depth — if you ever publish the orchestrator port directly for CI / scripts / curl, it's still gated. and it means dropping the UI doesn't suddenly leave an unauthed orchestrator running.

# open questions

these are the things i'm not sure about and want the team to weigh in on.

## 1. proxy choice

Caddy, nginx, traefik, or a tiny custom server (Node, Python, Go)?

- **Caddy:** single static binary, trivial config, native HTTPS if anyone wants it, native WS upgrade. probably the right answer.
- **nginx:** ubiquitous, well-understood, more verbose config.
- **custom (e.g. small Node server):** lets us colocate proxy logic and any UI-side helpers in one process. more code to maintain.

leaning Caddy. is anyone strongly opposed?

## 2. PWA stack

how much framework do we want? options span from:

- pure vanilla HTML/CSS/JS, no build step, no dependencies
- a tiny library (htmx? Alpine? Preact via CDN?)
- a full framework (SvelteKit, Vue, React)

a build step means a node toolchain in the image build, which is not free. but a 5,000-line vanilla SPA is also not free to maintain. what's the team's appetite?

## 3. how much UI scope for v1

minimum-viable list to discuss:

- list containers, with status
- view a container's metadata and recent logs
- stop / destroy a container
- list `drover/*` images
- launch a container from an image (form-driven `POST /containers`)

stretch (probably not v1):

- live log/stdout streaming (depends on the WebSocket work in @docs/planning/websocket-streaming-plan.md)
- kicking off builds (depends on the builder layer in @THOUGHTS.md)
- multi-orchestrator support (single UI managing several Drover hosts)

## 4. multi-orchestrator: ever?

the env-var-URL design supports one orchestrator per UI instance. if an operator runs multiple Drover hosts, they run multiple UI containers.

an alternative is letting the UI manage a list of orchestrators — but that pushes us back toward token-in-localStorage and a more complex UX. probably defer.

## 5. health & readiness

does the UI container expose its own `/health` (independent of the orchestrator), or does it just proxy `/api/health` and let the operator's monitoring use that? leaning toward its own `/health` so failures are distinguishable.

## 6. config endpoint for the PWA

the PWA may want to know things like "is auth enabled", "what's the orchestrator's privileged image", etc. options:

- the PWA hits `/api/health` (already exposed) for the bits it needs
- the UI container exposes a `/config` endpoint that returns UI-specific config
- the PWA is fully static and config-free

leaning option 1 for v1.

## 7. logging

does the UI container log every proxied request (noisy), only errors (loses visibility), or follow whatever the proxy does by default? probably "default" with a knob.

## 8. naming and image tag

the orchestrator publishes as `ghcr.io/saibotsivad/drover` (per `publish.yml`). this would be `ghcr.io/saibotsivad/drover-ui`? confirm the convention.

# alternatives considered

## shared-network, no orchestrator auth

the @TODO.md sketch: orchestrator has no auth at all and is unreachable except over a Docker network the UI shares with it. trust boundary is "who can join this Docker network."

rejected because:
- forces same-host operation
- removes a useful option (operator publishes orchestrator port directly for scripts/CI)
- the env-var-URL approach gets us the same isolation when operators want it (just don't publish the orchestrator port) without the lock-in

## PWA holds bearer token

keep token in localStorage, send from the browser, UI container is a dumb proxy. rejected because the proxy has access to the request stream anyway, so it adds UX overhead without adding security.

## skip the UI image, ship it as a static site

just publish the PWA to GitHub Pages or similar and tell operators to enable CORS. rejected because CORS work on the orchestrator is real, WebSockets get awkward, and operators would still need to handle the token themselves.

# related

- @TODO.md — "Different container auth" — earlier sketch
- @docs/planning/websocket-streaming-plan.md — streaming work that the UI will eventually consume
- @THOUGHTS.md — the builder layer; a future UI feature would expose builds
