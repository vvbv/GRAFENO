"""Tests of the web panel branding (server/branding.py)."""

from __future__ import annotations

import base64

import pytest

from grafeno import config as config_module
from grafeno import paths
from grafeno.config import WEB_THEMES
from grafeno.server import branding
from grafeno.server.actions import ApiError

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def _item(name: str, data: bytes) -> dict:
    return {"name": name, "data": base64.b64encode(data).decode()}


def test_normalize_instance_name() -> None:
    assert branding.normalize_instance_name("  Work\n\tlaptop  ") == "Work laptop"
    assert len(branding.normalize_instance_name("x" * 100)) == 60
    assert branding.normalize_instance_name(None) == ""


def test_normalize_theme() -> None:
    for theme in WEB_THEMES:
        assert branding.normalize_theme(theme) == theme
    assert branding.normalize_theme("PINK") == "pink"
    for bad in ("auto", "neon", None):
        assert branding.normalize_theme(bad) == ""


def test_decode_favicon_validates_type_size_and_signature() -> None:
    assert branding.decode_favicon(_item("x.png", PNG)) == (".png", PNG)
    with pytest.raises(ApiError) as exc:
        branding.decode_favicon(_item("x.svg", PNG))
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        branding.decode_favicon(_item("x.png", b"<html>"))
    assert exc.value.status == 400
    with pytest.raises(ApiError) as exc:
        branding.decode_favicon({"name": "x.png", "data": "not base64!"})
    assert exc.value.status == 400
    big = PNG + b"\x00" * branding.FAVICON_MAX_BYTES
    with pytest.raises(ApiError) as exc:
        branding.decode_favicon(_item("x.png", big))
    assert exc.value.status == 413
    webp = b"RIFF" + b"\x00" * 4 + b"WEBP"
    assert branding.decode_favicon(_item("x.webp", webp))[0] == ".webp"
    ico = b"\x00\x00\x01\x00" + b"\x00" * 8
    assert branding.decode_favicon(_item("x.ico", ico))[0] == ".ico"


def test_store_and_clear_favicon() -> None:
    assert branding.store_favicon(".png", PNG) == "favicon.png"
    assert (paths.web_dir() / "favicon.png").read_bytes() == PNG
    branding.store_favicon(".ico", b"\x00\x00\x01\x00")
    assert sorted(p.name for p in paths.web_dir().glob("favicon.*")) == ["favicon.ico"]
    branding.clear_favicon()
    assert not list(paths.web_dir().glob("favicon.*"))


def test_favicon_response() -> None:
    assert branding.favicon_response().status == 204
    cfg = config_module.load()
    cfg.web.favicon = branding.store_favicon(".png", PNG)
    config_module.save(cfg)
    response = branding.favicon_response()
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/png"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.body == PNG
    cfg.web.favicon = "../config.toml"
    config_module.save(cfg)
    assert branding.favicon_file() is None
    assert branding.favicon_response().status == 204
