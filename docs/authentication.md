# Authentication

Operator-facing reference for Drover's optional API authentication. Read
this when you want to lock down the orchestrator API, generate a key, or
understand how the token is verified.

Drover supports optional bearer-token authentication via the
`DROVER_API_KEY` environment variable. When set, every API request
(except `GET /health`) must include an `Authorization: Bearer <key>`
header. Requests without a valid token receive a `401 Unauthorized`
response.

This is designed as a simple security layer for homelab use. If you plan
to expose the API to the public internet, add additional layers of
security (a reverse proxy with TLS, IP allowlisting, a VPN, etc.). The
[webapp](../webapp/README.md) can also hold the token and inject it into
proxied requests, so end users never need direct access to it — see the
webapp's [trust boundary](../webapp/README.md#trust-boundary) notes.

---

## Setup

### 1. Generate a key and its hash

The orchestrator stores only the **SHA-256 hash** of the key; the
plain-text key is what callers send. You need both halves.

Generate a fresh key and hash with the included helper script:

```sh
python scripts/generate_api_key.py
```

Example output:

```
Plain-text key : m7x...Qf8
SHA-256 hash   : a1b2c3d4...

Set the hash as your environment variable:
  export DROVER_API_KEY="a1b2c3d4..."

Pass the plain-text key in API requests:
  curl -H 'Authorization: Bearer m7x...Qf8' http://localhost:8000/images
```

To hash an existing key instead of generating a new one:

```sh
python scripts/generate_api_key.py --key "my-secret-key"
```

The [`drover keygen`](cli.md#6-key-generation) CLI subcommand does the
same thing without needing a checkout of this repo, printing the key in
three ready-to-paste forms (orchestrator env var, raw `Authorization`
header, and CLI export).

### 2. Give the hash to the orchestrator

Pass the **hash** (not the plain-text key) via the `DROVER_API_KEY`
environment variable:

```sh
docker run -e DROVER_API_KEY="a1b2c3d4..." ...
```

### 3. Send the plain-text key with each request

Include the **plain-text key** in the `Authorization` header:

```sh
curl -H "Authorization: Bearer m7x...Qf8" http://localhost:8000/containers
```

The `drover` CLI reads the plain-text key from `DROVER_API_KEY` in its own
environment and sends it automatically — see [`docs/cli.md`](cli.md).

---

## How it works

The caller sends the plain-text API key in the `Authorization: Bearer`
header. The orchestrator hashes the provided key with SHA-256 and compares
it — using a constant-time comparison to avoid timing attacks — against the
pre-hashed value in `DROVER_API_KEY`. The plain-text key is never stored on
the server.

If `DROVER_API_KEY` is not set, authentication is disabled and all requests
are allowed. The orchestrator logs a warning at startup when authentication
is disabled, so an accidentally-open instance is visible in the logs.

The `GET /health` endpoint is always accessible without authentication, so
load balancers and monitoring tools can check availability without holding
a key.
