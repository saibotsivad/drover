"""Bearer token authentication middleware.

When the DROVER_API_KEY environment variable is set, all API requests (except
GET /health) must include an ``Authorization: Bearer <token>`` header.  The
token is hashed with SHA-256 and compared against the pre-hashed value stored
in DROVER_API_KEY.  If the variable is not set, authentication is disabled.
"""

import hashlib
import hmac
import logging

from fastapi import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

# Paths that never require authentication.
_PUBLIC_PATHS: set[str] = {"/health"}


def hash_api_key(plain_key: str) -> str:
    """Return the SHA-256 hex digest of *plain_key*.

    This is the same algorithm callers should use to produce the value
    stored in the ``DROVER_API_KEY`` environment variable.
    """
    return hashlib.sha256(plain_key.encode()).hexdigest()


async def auth_middleware(request: Request, call_next) -> Response:
    """FastAPI middleware that enforces bearer-token authentication."""
    api_key_hash: str | None = request.app.state.config.api_key_hash

    # Auth disabled — pass through.
    if api_key_hash is None:
        return await call_next(request)

    # Public endpoints are always accessible.
    if request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or malformed Authorization header"},
        )

    token = auth_header[7:]  # strip "Bearer "
    token_hash = hash_api_key(token)

    if not hmac.compare_digest(token_hash, api_key_hash):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid API key"},
        )

    return await call_next(request)
