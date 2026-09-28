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
from ..i18n import current_language, t
from ..models import TaskState
from .httpcore import Request, Response

DEFAULT_WEB_HOST = "127.0.0.1"
PAGE_PATHS = ("/", "/index.html")
FAVICON_PATH = "/favicon.ico"
WILDCARD_HOSTS = ("0.0.0.0", "::", "")
_I18N_MARKER = "/*__GRAFENO_I18N__*/null"

# Catalog keys the page needs (the page receives them already translated).
UI_KEYS = (
    "web.ui.title", "web.ui.new_task", "web.ui.search", "web.ui.all_states",
    "web.ui.all_projects", "web.ui.hide_done", "web.ui.col_name",
    "web.ui.col_project", "web.ui.col_state", "web.ui.empty", "web.ui.select",
    "web.ui.back", "web.ui.live", "web.ui.polling", "web.ui.offline",
    "web.ui.token_title", "web.ui.token_help", "web.ui.token_label",
    "web.ui.token_submit", "web.ui.logout", "web.ui.tab_info",
    "web.ui.tab_description", "web.ui.tab_log", "web.ui.tab_first",
    "web.ui.tab_plan", "web.ui.tab_review", "web.ui.tab_final", "web.ui.cycle",
    "web.ui.no_files", "web.ui.no_log", "web.ui.refresh", "web.ui.start",
    "web.ui.resume", "web.ui.restart", "web.ui.pause", "web.ui.discard",
    "web.ui.mark_done", "web.ui.extend", "web.ui.confirm_restart",
    "web.ui.confirm_discard", "web.ui.confirm_mark_done", "web.ui.extend_title",
    "web.ui.extend_label", "web.ui.cancel", "web.ui.submit", "web.ui.name",
    "web.ui.workdir", "web.ui.description", "web.ui.profile",
    "web.ui.profile_help", "web.ui.parent", "web.ui.parent_none",
    "web.ui.automode", "web.ui.start_now", "web.ui.attachments",
    "web.ui.created", "web.ui.done_ok", "web.ui.error", "web.ui.field_id",
    "web.ui.field_state", "web.ui.field_workdir", "web.ui.field_remote",
    "web.ui.field_profile", "web.ui.field_origin", "web.ui.field_automode",
    "web.ui.field_cycle", "web.ui.field_iteration", "web.ui.field_branch",
    "web.ui.field_scheduled", "web.ui.field_parent", "web.ui.field_failed_phase",
    "web.ui.field_tokens", "web.ui.field_duration", "web.ui.field_created",
    "web.ui.field_updated", "web.ui.field_roles", "web.ui.yes", "web.ui.no",
    "web.ui.transcription_failed",
)


@dataclass
class WebLaunch:
    """Web panel settings for one run (never persisted)."""

    host: str = DEFAULT_WEB_HOST
    port: int = 0
    token: str = ""  # ephemeral token generated for this run ("" = none)

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


def prepare(api: ApiConfig, host: str = "", port: int = 0) -> tuple[ApiConfig, WebLaunch]:
    """Return the API config for a ``--web`` run and its launch settings.

    ``host`` empty means loopback only; ``port`` 0 keeps the configured API
    port. The returned config is a copy: the saved ``config.toml`` is never
    modified. Exposing the panel beyond loopback without any configured
    token generates an ephemeral one so the panel is never open to the
    network without authentication.
    """
    host = (host or DEFAULT_WEB_HOST).strip()
    effective = replace(api, enabled=True, host=host, port=port or api.port)
    launch = WebLaunch(host=host, port=effective.port)
    if not is_loopback(host) and not api.resolve_tokens():
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
    return request.method == "GET" and request.path in PAGE_PATHS + (FAVICON_PATH,)


def _catalog() -> dict[str, str]:
    strings = {key: t(key) for key in UI_KEYS}
    for state in TaskState:
        strings[f"state.{state.value}"] = t(f"state.{state.value}")
    return strings


def render_page() -> str:
    """The panel HTML with the translated strings of the active language."""
    template = resources.files(__package__).joinpath("static/index.html").read_text(encoding="utf-8")
    data = {
        "lang": current_language(),
        "version": __version__,
        "states": [state.value for state in TaskState],
        "strings": _catalog(),
    }
    # "</" escaped so no string can close the <script> element early.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return template.replace(_I18N_MARKER, payload).replace("__GRAFENO_LANG__", current_language())


def page_response(request: Request) -> Response:
    """Serve the panel (or an empty favicon answer)."""
    if request.path == FAVICON_PATH:
        return Response(204)
    headers = {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",  # the URL may carry ?token=
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(200, headers=headers, body=render_page().encode("utf-8"))
