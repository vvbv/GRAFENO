"""Web administration panel served by the API server.

``grafeno --web`` starts the REST + WebSocket server for this run (without
touching ``config.toml``) and the server exposes a single-page panel at
``/`` that drives GRAFENO through the same REST/WS API. The page itself is
static (no task data), so it is served without authentication; every data
request it makes goes through the regular token check.

Binding defaults to loopback. When the panel is opened to other hosts
(e.g. ``--web-host 0.0.0.0``) and no API token is configured, an ephemeral
token is generated for the run: it only lives in memory, is embedded in
the URL announced in the TUI and is never written to disk nor logged.
"""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
from dataclasses import dataclass, replace
from importlib import resources
from urllib.parse import quote

from .. import __version__
from ..config import ApiConfig
from ..i18n import catalog, current_language
from ..models import TaskState
from .httpcore import Request, Response

DEFAULT_WEB_HOST = "127.0.0.1"
PAGE_PATHS = ("/", "/index.html")
FAVICON_PATH = "/favicon.ico"
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
_I18N_MARKER = "/*__GRAFENO_I18N__*/null"

ASSET_PREFIX = "/assets/"
# Static assets of the panel (served without auth, like the page itself).
ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
# Catalog key prefixes the page needs (sent already translated): the panel
# reuses the TUI texts (new-task form, detail, settings, reports...) and
# adds its own ``web.*`` keys.
UI_PREFIXES = (
    "web.", "state.", "phase.", "common.", "tasks.", "nt.", "det.", "act.",
    "pc.", "pconf.", "phaseinfo.", "rm.", "et.", "roles.", "cfg.", "hook.",
    "refs.", "trig.", "prof.", "reports.", "media.",
)
# The page only loads its own assets; inline scripts are not allowed (the
# boot data travels as a non-executable JSON block).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' blob: data:; media-src 'self' blob:; "
    "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@dataclass
class WebLaunch:
    """Web panel settings for one run (never persisted)."""

    host: str = DEFAULT_WEB_HOST
    port: int = 0
    token: str = ""  # ephemeral token generated for this run ("" = none)
    noauth: bool = False  # authentication disabled for this run (--noauth)

    def urls(self, port: int | None = None) -> list[str]:
        """Panel URLs to announce (with the ephemeral token, if any)."""
        return panel_urls(self.host, self.port if port is None else port, self.token)


def is_loopback(host: str) -> bool:
    """True when ``host`` only accepts local connections."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def prepare(
    api: ApiConfig, host: str = "", port: int = 0, noauth: bool = False
) -> tuple[ApiConfig, WebLaunch]:
    """Return the API config for a ``--web`` run and its launch settings.

    ``host`` empty means loopback only; ``port`` 0 keeps the configured API
    port. The returned config is a copy: the saved ``config.toml`` is never
    modified. Exposing the panel beyond loopback without any configured
    token generates an ephemeral one so the panel is never open to the
    network without authentication, unless ``noauth`` explicitly disables
    authentication for the run (configured and environment tokens are then
    ignored too).
    """
    host = (host or DEFAULT_WEB_HOST).strip()
    effective = replace(api, enabled=True, host=host, port=port or api.port, auth_disabled=noauth)
    launch = WebLaunch(host=host, port=effective.port, noauth=noauth)
    if not noauth and not is_loopback(host) and not api.resolve_tokens():
        launch.token = secrets.token_urlsafe(24)
        effective.tokens = launch.token
    return effective, launch


def _lan_address() -> str:
    """Best-effort primary LAN address (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
    except OSError:
        return ""
    return "" if address.startswith("127.") else address


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def panel_urls(host: str, port: int, token: str = "") -> list[str]:
    """URLs where the panel is reachable (wildcard binds list local + LAN)."""
    if host.strip() in WILDCARD_HOSTS:
        hosts = ["127.0.0.1"]
        lan = _lan_address()
        if lan:
            hosts.append(lan)
    else:
        hosts = [host.strip()]
    suffix = f"?token={quote(token, safe='')}" if token else ""
    return [f"http://{_url_host(item)}:{port}/{suffix}" for item in hosts]


def is_page_request(request: Request) -> bool:
    """Requests answered by the panel itself (served without auth)."""
    if request.method != "GET":
        return False
    if request.path in PAGE_PATHS + (FAVICON_PATH,):
        return True
    return request.path.startswith(ASSET_PREFIX) and request.path[len(ASSET_PREFIX):] in ASSETS


def _static(name: str) -> str:
    return resources.files(__package__).joinpath(f"static/{name}").read_text(encoding="utf-8")


def render_page() -> str:
    """The panel HTML with the translated strings of the active language."""
    data = {
        "lang": current_language(),
        "version": __version__,
        "states": [state.value for state in TaskState],
        "strings": catalog(UI_PREFIXES),
    }
    # Every "<" escaped so no string can close (or comment out) the
    # <script> block that carries the JSON.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return (
        _static("index.html")
        .replace(_I18N_MARKER, payload)
        .replace("__GRAFENO_LANG__", current_language())
        .replace("__GRAFENO_VERSION__", __version__)
    )


def page_response(request: Request) -> Response:
    """Serve the panel, one of its assets or an empty favicon answer."""
    if request.path == FAVICON_PATH:
        return Response(204)
    headers = {
        "Referrer-Policy": "no-referrer",  # the URL may carry ?token=
        "X-Content-Type-Options": "nosniff",
    }
    if request.path.startswith(ASSET_PREFIX):
        name = request.path[len(ASSET_PREFIX):]
        headers["Content-Type"] = ASSETS[name]
        headers["Cache-Control"] = "no-cache"
        return Response(200, headers=headers, body=_static(name).encode("utf-8"))
    headers.update({
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": CSP,
    })
    return Response(200, headers=headers, body=render_page().encode("utf-8"))
