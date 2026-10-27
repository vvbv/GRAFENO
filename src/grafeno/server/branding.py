"""Web panel branding: instance name, theme and custom favicon.

The values live in the ``[web]`` section of ``config.toml`` and the favicon
file under ``~/.grafeno/web/``. Only raster formats are accepted (no SVG:
the panel origin never serves active content) and the file signature must
match its extension.
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from .. import config as config_module
from .. import media
from .. import paths
from ..config import WEB_THEMES, WebConfig
from ..i18n import t
from .actions import ApiError
from .httpcore import Response

INSTANCE_NAME_MAX = 60  # characters
FAVICON_MAX_BYTES = 256 * 1024
FAVICON_STEM = "favicon"
FAVICON_TYPES = {
    ".png": "image/png", ".ico": "image/x-icon", ".gif": "image/gif",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
}


def normalize_instance_name(raw: object) -> str:
    """Printable, single-spaced instance name capped at ``INSTANCE_NAME_MAX``."""
    text = "".join(ch for ch in str(raw or "") if ch.isprintable() or ch.isspace())
    text = " ".join(text.split())
    return text[:INSTANCE_NAME_MAX].strip()


def normalize_theme(raw: object) -> str:
    """A known theme name, or ``""`` (automatic) for anything else."""
    value = str(raw or "").strip().lower()
    return value if value in WEB_THEMES else ""


def _matches_signature(suffix: str, data: bytes) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix == ".ico":
        return data.startswith(b"\x00\x00\x01\x00")
    if suffix == ".gif":
        return data.startswith(b"GIF87a") or data.startswith(b"GIF89a")
    if suffix in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def decode_favicon(item: object) -> tuple[str, bytes]:
    """Validate an uploaded ``{"name", "data"}`` favicon; return (suffix, bytes)."""
    if not isinstance(item, dict) or not isinstance(item.get("data"), str):
        raise ApiError(400, t("cfg.error.favicon_data"))
    suffix = Path(str(item.get("name") or "")).suffix.lower()
    if suffix not in FAVICON_TYPES:
        raise ApiError(400, t("cfg.error.favicon_type"))
    too_large = ApiError(413, t("cfg.error.favicon_size", size=FAVICON_MAX_BYTES // 1024))
    if len(item["data"]) > media.base64_length(FAVICON_MAX_BYTES) + 4:
        raise too_large  # too long to be valid: refuse without decoding
    try:
        raw = base64.b64decode(item["data"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, t("cfg.error.favicon_data")) from exc
    if len(raw) > FAVICON_MAX_BYTES:
        raise too_large
    if not raw or not _matches_signature(suffix, raw):
        raise ApiError(400, t("cfg.error.favicon_data"))
    return suffix, raw


def clear_favicon() -> None:
    """Delete any stored favicon (best effort)."""
    directory = paths.web_dir()
    for suffix in (*FAVICON_TYPES, ".tmp"):
        try:
            (directory / (FAVICON_STEM + suffix)).unlink(missing_ok=True)
        except OSError:
            pass


def store_favicon(suffix: str, data: bytes) -> str:
    """Replace the stored favicon; return its file name (e.g. ``favicon.png``)."""
    clear_favicon()
    directory = paths.web_dir()
    tmp = directory / (FAVICON_STEM + ".tmp")
    tmp.write_bytes(data)
    name = FAVICON_STEM + suffix
    os.replace(tmp, directory / name)
    return name


def favicon_file(web: WebConfig | None = None) -> Path | None:
    """Path of the configured favicon, or None when absent or not a known name."""
    web = web or config_module.load().web
    if not web.favicon:
        return None
    for suffix in FAVICON_TYPES:  # compare against known names: no path traversal
        if web.favicon == FAVICON_STEM + suffix:
            path = paths.home() / "web" / (FAVICON_STEM + suffix)  # read-only: do not create the directory
            return path if path.is_file() else None
    return None


def favicon_version(web: WebConfig | None = None) -> int:
    """Cache-busting version of the favicon (mtime in ms; 0 = none).

    Milliseconds, not nanoseconds: the value must stay below 2**53 to
    survive a round trip through JavaScript numbers.
    """
    path = favicon_file(web)
    if path is None:
        return 0
    try:
        return int(path.stat().st_mtime_ns // 1_000_000)
    except OSError:
        return 0


def boot_info(web: WebConfig | None = None) -> dict:
    """Branding data embedded in the page boot JSON."""
    web = web or config_module.load().web
    return {
        "instance_name": web.instance_name,
        "theme": web.theme,
        "favicon_version": favicon_version(web),
    }


def instance_name() -> str:
    """Configured instance name ("" when not set)."""
    return config_module.load().web.instance_name


def favicon_response() -> Response:
    """The custom favicon, or 204 when none is configured."""
    path = favicon_file()
    if path is None:
        return Response(204)
    try:
        body = path.read_bytes()
    except OSError:
        return Response(204)
    return Response(200, headers={
        "Content-Type": FAVICON_TYPES[path.suffix.lower()],
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
    }, body=body)
