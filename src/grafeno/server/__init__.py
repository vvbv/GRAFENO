"""Remote API server: REST + WebSocket clients for GRAFENO.

The server uses only the stdlib (asyncio, base64, hashlib, struct, json,
urllib.parse) to avoid new dependencies. It runs as a background worker
inside the TUI, started by ``GrafenoApp`` when ``Config.api.enabled`` is
true.
"""

from __future__ import annotations

from .service import ServerService

__all__ = ["ServerService"]
