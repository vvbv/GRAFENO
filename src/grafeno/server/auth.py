"""Token authentication for the API server.

When tokens are configured, every request (REST and WebSocket upgrade)
must carry the token as ``Authorization: Bearer <token>`` header or
``?token=<token>`` query parameter. An empty configured token set means
NO authentication is required (open access).
"""

from __future__ import annotations

from ..config import ApiConfig


class AuthError(Exception):
    """Raised when authentication fails (mapped to HTTP 401)."""


def check(config: ApiConfig, headers: dict[str, str], query: dict[str, str] | None) -> None:
    """Validate the bearer token. Case-insensitive header names.

    With no tokens configured the server runs open (no authentication is
    required) and this function returns immediately. Otherwise it raises
    :class:`AuthError` when the provided token is not in the accepted set.
    The token itself is never logged.
    """
    tokens = config.resolve_tokens()
    if not tokens:
        return
    lowered = {key.lower(): value for key, value in headers.items()}
    provided = ""
    auth = lowered.get("authorization", "")
    if auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    elif query and query.get("token"):
        provided = query["token"]
    if provided not in tokens:
        raise AuthError("invalid token")
