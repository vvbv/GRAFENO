"""Token authentication for the API server.

Every request (REST and WebSocket upgrade) must carry the token as
``Authorization: Bearer <token>`` header or ``?token=<token>`` query
parameter. Empty configured token set means deny everything.
"""

from __future__ import annotations

from ..config import ApiConfig


class AuthError(Exception):
    """Raised when authentication fails (mapped to HTTP 401)."""


def check(config: ApiConfig, headers: dict[str, str], query: dict[str, str] | None) -> None:
    """Validate the bearer token. Case-insensitive header names.

    Raises :class:`AuthError` when no tokens are configured (deny all) or
    when the provided token is not in the accepted set. The token itself
    is never logged.
    """
    tokens = config.resolve_tokens()
    if not tokens:
        raise AuthError("no tokens configured")
    lowered = {key.lower(): value for key, value in headers.items()}
    provided = ""
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    elif query and query.get("token"):
        provided = query["token"]
    if provided not in tokens:
        raise AuthError("invalid token")
