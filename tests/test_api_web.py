"""Tests of the web administration panel (``grafeno --web``)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from grafeno import i18n
from grafeno.config import ApiConfig
from grafeno.server import web
from grafeno.server.service import ServerService


# ---------------------------------------------------------------------- #
# Launch settings: loopback by default, token when exposed
# ---------------------------------------------------------------------- #
def test_prepare_defaults_to_loopback_without_token(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    saved = ApiConfig()
    effective, launch = web.prepare(saved)
    assert effective.enabled is True
    assert effective.host == "127.0.0.1"
    assert effective.port == saved.port
    assert launch.token == ""
    assert effective.tokens == ""
    assert saved.enabled is False  # the saved config is never mutated


def test_prepare_wildcard_generates_ephemeral_token(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    saved = ApiConfig()
    effective, launch = web.prepare(saved, "0.0.0.0", 9100)
    assert effective.host == "0.0.0.0"
    assert effective.port == 9100
    assert len(launch.token) >= 24
    assert effective.resolve_tokens() == {launch.token}
    assert saved.tokens == ""


def test_prepare_exposed_keeps_configured_tokens(monkeypatch):
    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    effective, launch = web.prepare(ApiConfig(tokens="mine"), "0.0.0.0")
    assert launch.token == ""
    assert effective.resolve_tokens() == {"mine"}


def test_prepare_exposed_keeps_env_token(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")
    effective, launch = web.prepare(ApiConfig(), "0.0.0.0")
    assert launch.token == ""
    assert effective.resolve_tokens() == {"from-env"}


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("::1", True), ("0.0.0.0", False),
     ("192.168.1.10", False), ("myhost", False)],
)
def test_is_loopback(host, expected):
    assert web.is_loopback(host) is expected


def test_panel_urls(monkeypatch):
    monkeypatch.setattr(web, "_lan_address", lambda: "10.0.0.5")
    assert web.panel_urls("127.0.0.1", 8735) == ["http://127.0.0.1:8735/"]
    assert web.panel_urls("::1", 8735) == ["http://[::1]:8735/"]
    assert web.panel_urls("0.0.0.0", 80, "a b") == [
        "http://127.0.0.1:80/?token=a%20b",
        "http://10.0.0.5:80/?token=a%20b",
    ]


# ---------------------------------------------------------------------- #
# Page rendering
# ---------------------------------------------------------------------- #
def test_render_page_injects_translated_strings():
    html = web.render_page()
    assert "__GRAFENO_I18N__" not in html
    assert "__GRAFENO_LANG__" not in html
    assert "__GRAFENO_VERSION__" not in html
    assert '"web.nav.new": "New task"' in html
    assert '"nt.repeat": "Repetitive task"' in html  # TUI texts are reused
    assert '<html lang="en">' in html
    i18n.set_language("es")
    html = web.render_page()
    assert '"web.nav.new": "Nueva tarea"' in html
    assert '<html lang="es">' in html


def test_render_page_escapes_script_breakers(monkeypatch):
    monkeypatch.setitem(i18n._MESSAGES["en"], "web.ui.evil", "</script><!--")
    html = web.render_page()
    assert "</script><!--" not in html
    assert "\\u003c/script>\\u003c!--" in html


def test_page_strings_exist_in_both_languages():
    """Every key the panel script uses is translated in en and es."""
    import re
    from importlib import resources

    script = resources.files("grafeno.server").joinpath("static/app.js").read_text(encoding="utf-8")
    keys = set(re.findall(r'tr\("([a-z_.]+)"', script))
    assert keys
    for key in keys:
        if key.endswith("."):
            continue  # dynamic suffix (state., phase.)
        assert key.startswith(web.UI_PREFIXES), key
        assert key in i18n._MESSAGES["en"], key
        assert key in i18n._MESSAGES["es"], key


# ---------------------------------------------------------------------- #
# Served page: no auth for the page, auth for the data
# ---------------------------------------------------------------------- #
async def _get(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode("ascii"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        lines = head.decode("iso-8859-1").strip().split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0) if length else b""
    finally:
        writer.close()
        await writer.wait_closed()
    return int(lines[0].split(" ")[1]), headers, body


def test_server_serves_page_without_token_and_protects_api():
    async def scenario():
        app = MagicMock()
        launch = web.WebLaunch(host="127.0.0.1", port=0, token="secret")
        service = ServerService(
            ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="secret"),
            app=app, web=launch,
        )
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, headers, body = await _get(service.port, "/")
            assert status == 200
            assert headers["content-type"].startswith("text/html")
            assert headers["referrer-policy"] == "no-referrer"
            assert b"GRAFENO" in body
            assert "script-src 'self'" in headers["content-security-policy"]
            status, headers, body = await _get(service.port, "/assets/app.js")
            assert status == 200
            assert headers["content-type"].startswith("text/javascript")
            assert b"grafeno-boot" in body
            status, headers, _ = await _get(service.port, "/assets/app.css")
            assert status == 200 and headers["content-type"].startswith("text/css")
            status, _, _ = await _get(service.port, "/assets/other.js")
            assert status == 401  # only the known assets bypass auth
            status, _, _ = await _get(service.port, "/favicon.ico")
            assert status == 204
            status, _, _ = await _get(service.port, "/api/v1/tasks")
            assert status == 401
            status, _, _ = await _get(service.port, "/api/v1/tasks?token=secret")
            assert status == 200
        finally:
            service.stop()
            task.cancel()
        messages = [call.args[0] for call in app.notify.call_args_list]
        assert f"Web panel: http://127.0.0.1:{service.port}/?token=secret" in messages

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# Command line
# ---------------------------------------------------------------------- #
def _run_main(monkeypatch, argv: list[str]) -> MagicMock:
    from grafeno import app as app_module

    monkeypatch.delenv("GRAFENO_API_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["grafeno", "--noeditor", *argv])
    app_mock = MagicMock()
    monkeypatch.setattr(app_module, "GrafenoApp", app_mock)
    app_module.main()
    return app_mock


def test_main_without_web_keeps_saved_api(monkeypatch):
    app_mock = _run_main(monkeypatch, [])
    assert app_mock.call_args.kwargs == {"web": None, "api_config": None}


def test_main_web_defaults_to_localhost(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--web"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].enabled is True
    assert kwargs["api_config"].host == "127.0.0.1"
    assert kwargs["web"].token == ""


def test_main_web_host_implies_web(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--web-host", "0.0.0.0", "--web-port", "9001"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].host == "0.0.0.0"
    assert kwargs["api_config"].port == 9001
    assert kwargs["web"].token


def test_main_rejects_bad_web_port(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, ["--web-port", "70000"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------- #
# --no-auth
# ---------------------------------------------------------------------- #
def test_prepare_no_auth_ignores_every_token(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")
    saved = ApiConfig(tokens="mine")
    effective, launch = web.prepare(saved, "0.0.0.0", no_auth=True)
    assert launch.no_auth is True
    assert launch.token == ""  # no temporary token either
    assert effective.resolve_tokens() == set()
    assert "auth_disabled" not in effective.to_dict()  # never persisted
    assert saved.resolve_tokens() == {"from-env", "mine"}  # saved config untouched


def test_main_no_auth_implies_web(monkeypatch):
    app_mock = _run_main(monkeypatch, ["--no-auth"])
    kwargs = app_mock.call_args.kwargs
    assert kwargs["api_config"].enabled is True
    assert kwargs["api_config"].host == "127.0.0.1"
    assert kwargs["api_config"].auth_disabled is True
    assert kwargs["web"].no_auth is True


def test_server_no_auth_accepts_requests_and_warns(monkeypatch):
    monkeypatch.setenv("GRAFENO_API_TOKEN", "from-env")

    async def scenario():
        app = MagicMock()
        api_cfg, launch = web.prepare(ApiConfig(port=0, tokens="secret"), "0.0.0.0", no_auth=True)
        service = ServerService(api_cfg, app=app, web=launch)
        task = asyncio.create_task(service.run())
        for _ in range(200):
            if service.server is not None:
                break
            await asyncio.sleep(0.01)
        try:
            status, _, _ = await _get(service.port, "/api/v1/tasks")
            assert status == 200
        finally:
            service.stop()
            task.cancel()
        calls = app.notify.call_args_list
        warning = next(call for call in calls if call.args[0] == i18n.t("web.no_auth_exposed"))
        assert warning.kwargs["severity"] == "error"
        assert not any("?token=" in call.args[0] for call in calls)

    asyncio.run(scenario())
