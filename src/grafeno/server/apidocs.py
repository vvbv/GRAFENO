"""Self-hosted, localized Swagger UI and packaged API documentation resources."""

from __future__ import annotations

import html
import re
from importlib import resources

from .. import __version__, _toml
from ..i18n import DEFAULT_LANGUAGE, LANGUAGES, current_language, t_lang
from .httpcore import Request, Response

PAGE_PATHS = ("/docs", "/docs/", "/docs/index.html")
SPEC_PATHS = {
    "/docs/openapi.json": "openapi",
    "/docs/asyncapi.json": "asyncapi",
}
ASSET_PREFIX = "/docs/assets/"
ASSETS = {
    "docs.css": ("text/css; charset=utf-8", "static/docs.css"),
    "docs.js": ("text/javascript; charset=utf-8", "static/docs.js"),
    "swagger-ui.css": ("text/css; charset=utf-8", "static/swagger-ui/swagger-ui.css"),
    "swagger-ui-bundle.js": (
        "text/javascript; charset=utf-8",
        "static/swagger-ui/swagger-ui-bundle.js",
    ),
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; "
    "base-uri 'none'; form-action 'self'"
)
SKILL_NAME_PLACEHOLDER = '"<served-instance-name>"'
SKILL_URL_PLACEHOLDER = '"<served-instance-url>"'
_HOST_HEADER = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?"
)


def normalize_language(raw: str) -> str:
    """Return a supported documentation language without changing global state."""
    return raw if raw in LANGUAGES else DEFAULT_LANGUAGE


def request_language(request: Request) -> str:
    """Resolve the explicit language or the server's current GUI language."""
    raw = request.query.get("lang", "")
    return normalize_language(raw) if raw else current_language()


def is_docs_request(request: Request) -> bool:
    """Recognize only the exact public documentation routes and asset names."""
    if request.method != "GET":
        return False
    if request.path in PAGE_PATHS or request.path in SPEC_PATHS:
        return True
    return request.path.startswith(ASSET_PREFIX) and request.path[len(ASSET_PREFIX):] in ASSETS


def docs_response(request: Request) -> Response:
    """Serve the documentation shell, a localized spec or a whitelisted asset."""
    headers = {"X-Content-Type-Options": "nosniff"}
    if request.path in PAGE_PATHS:
        headers.update({
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store",
            "Content-Security-Policy": CSP,
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        })
        return Response(200, headers=headers, body=render_page(request_language(request)).encode("utf-8"))
    if request.path in SPEC_PATHS:
        return spec_response(SPEC_PATHS[request.path], request_language(request))
    name = request.path[len(ASSET_PREFIX):]
    content_type, resource = ASSETS[name]
    headers.update({"Content-Type": content_type, "Cache-Control": "no-cache"})
    body = resources.files(__package__).joinpath(resource).read_bytes()
    return Response(200, headers=headers, body=body)


def render_page(language: str) -> str:
    """Render the translated shell using escaped, non-executable placeholders."""
    language = normalize_language(language)
    values = {
        "__GRAFENO_LANG__": language,
        "__GRAFENO_VERSION__": __version__,
        "__DOCS_TITLE__": t_lang(language, "docs.ui.title"),
        "__DOCS_LANGUAGE__": t_lang(language, "docs.ui.language"),
        "__DOCS_WEBSOCKET__": t_lang(language, "docs.ui.websocket"),
        "__DOCS_SKILL__": t_lang(language, "docs.ui.skill"),
        "__DOCS_HELP__": t_lang(language, "docs.ui.help"),
        "__OPENAPI_URL__": f"/docs/openapi.json?lang={language}",
        "__ASYNCAPI_URL__": f"/docs/asyncapi.json?lang={language}",
    }
    page = resources.files(__package__).joinpath("static/docs.html").read_text(encoding="utf-8")
    for placeholder, value in values.items():
        page = page.replace(placeholder, html.escape(value, quote=True))
    return page


def spec_response(kind: str, language: str) -> Response:
    """Serve one pre-generated JSON specification selected from fixed choices."""
    language = normalize_language(language)
    resource = f"static/docs/{kind}.{language}.json"
    body = resources.files(__package__).joinpath(resource).read_bytes()
    return Response(200, headers={
        "Content-Type": "application/json; charset=utf-8",
        "Content-Language": language,
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }, body=body)


def skill_text() -> str:
    """Read the packaged API skill without altering its Markdown."""
    return resources.files(__package__).joinpath("static/skill.md").read_text(encoding="utf-8")


def render_skill(text: str, name: str, host: str) -> str:
    """Fill the skill's example entry; an unusable value keeps its placeholder.

    ``name`` must already be normalized. ``host`` comes from the request, so
    anything but a plain host[:port] is ignored. The scheme is always http:
    the server does not know about a TLS proxy in front of it, and the skill
    tells the reader to check it. Tokens never reach this function.
    """
    if _HOST_HEADER.fullmatch(host):
        text = text.replace(SKILL_URL_PLACEHOLDER, _toml._format_value(f"http://{host}"))
    if name:
        text = text.replace(SKILL_NAME_PLACEHOLDER, _toml._format_value(name))
    return text
