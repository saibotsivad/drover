# ADR: WebSockets for command output streaming

**Date:** 2026-04-11
**Status:** Accepted

## Context

The current exec API uses polling (`GET /containers/{id}/exec/{cmd_id}`) to retrieve command output. For long-running commands this creates unnecessary latency and load. The natural replacement is a push-based streaming approach.

Two standard options exist:

- **Server-Sent Events (SSE):** a persistent HTTP response that streams text chunks from server to client. One-directional by design.
- **WebSockets:** a full-duplex protocol established via an HTTP `Upgrade` handshake. Bidirectional, but works fine with one-directional use.

The question of bidirectionality (e.g. sending stdin to a running command) is not an immediate requirement, but the more significant question for this decision was network topology behaviour.

## Decision

We will use **WebSockets** for command output streaming.

## Reasoning

### SSE's "simpler" label is protocol-level, not deployment-level

SSE is simpler to describe: it is just a long-lived HTTP response with a particular content type and line format. At the protocol and server-implementation level this is true.

However, HTTP intermediaries — reverse proxies, load balancers, CDN edges — commonly buffer response bodies by default, waiting for the response to complete before forwarding. This silently breaks SSE. The fix (e.g. `proxy_buffering off` in nginx) is simple once you know it is needed, but it is easy to omit and produces confusing failures with no obvious error message.

A homelab deployment almost always has a reverse proxy in front (nginx, Caddy, Traefik). Requiring users to configure buffering correctly, and documenting that clearly, is a non-trivial operational burden.

### WebSockets behave more consistently through intermediaries

WebSockets use the `Upgrade` header to switch protocols. Once upgraded, intermediaries treat the connection as an opaque TCP tunnel and pass data through without buffering. Proxies that support WebSocket at all handle it correctly. The required proxy configuration (`proxy_http_version 1.1`, forwarding `Upgrade` and `Connection` headers) is minimal, widely documented, and commonly done — most homelab users will have already done it for other services.

### HTTP/2 rescues SSE, but end-to-end H2 is not guaranteed

HTTP/2 multiplexing resolves SSE's connection-limit and proxy-buffering problems. However, end-to-end HTTP/2 (browser → proxy → backend) is not the default: most reverse proxies terminate H2 from the browser and speak HTTP/1.1 to backends. Relying on HTTP/2 to make SSE behave correctly would be an invisible assumption.

### Bidirectionality is a free option, not a reason to avoid WebSockets

Choosing WebSockets does not commit us to building bidirectional features, but it leaves the door open without a protocol migration. If we later want to send stdin to a running command, attach to an interactive shell, or deliver cancellation signals, the transport already supports it. Migrating from SSE to WebSockets later would require changing both server and client code.

## Consequences

- Command output streaming will be implemented over a WebSocket endpoint.
- Deployment documentation must cover the reverse proxy configuration needed to forward the `Upgrade`/`Connection` headers. This is the main operational cost of this decision, but it is a well-understood one-time config.
- The SSE option is not foreclosed forever; it could still be offered as an alternative for callers that cannot use WebSockets, but it is not the primary implementation target.
