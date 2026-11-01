"""HTTP and packaged-resource tests for the multilingual API documentation."""

from __future__ import annotations

import asyncio
import json
from importlib import resources

import yaml

from grafeno import i18n
from grafeno.config import ApiConfig
from grafeno.server import apidocs
from grafeno.server.service import ServerService


async def _start_service() -> tuple[ServerService, asyncio.Task]:
    config = ApiConfig(enabled=True, host="127.0.0.1", port=0, tokens="t1")
    service = ServerService(config, app=None)
    task = asyncio.create_task(service.run())
    for _ in range(200):
        if service.server is not None:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("server did not bind in time")
    await asyncio.sleep(0)
    return service, task


def _stop(service: ServerService, task: asyncio.Task) -> None:
    if service.http is not None:
        service.http.close()
    if not task.done():
        task.cancel()


async def _request(
    service: ServerService,
    path: str,
    *,
    token: str = "",
    bearer: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    headers = ["Host: 127.0.0.1", "Connection: close"]
    if bearer:
        headers.append("Authorization: Bearer t1")
    if token:
        separator = "&" if "?" in path else "?"
        path = f"{path}{separator}token={token}"
    raw = (f"GET {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n").encode("ascii")
    reader, writer = await asyncio.open_connection("127.0.0.1", service.port)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ", 2)[1])
        response_headers = {}
        for line in lines[1:]:
            key, separator, value = line.partition(":")
            if separator:
                response_headers[key.strip().lower()] = value.strip()
        length = int(response_headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), timeout=5.0) if length else b""
        return status, response_headers, body
    finally:
        writer.close()
        await writer.wait_closed()


def test_documentation_shell_languages_and_aliases() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            for language, title, help_text in (
                ("en", "GRAFENO API documentation", "Choose English or Spanish"),
                ("es", "Documentación de la API de GRAFENO", "Elige inglés o español"),
            ):
                status, headers, body = await _request(service, f"/docs?lang={language}")
                text = body.decode("utf-8")
                assert status == 200
                assert headers["content-type"] == "text/html; charset=utf-8"
                assert headers["cache-control"] == "no-store"
                assert headers["x-frame-options"] == "DENY"
                assert "script-src 'self'" in headers["content-security-policy"]
                assert "unsafe-inline" not in headers["content-security-policy"]
                assert f'<html lang="{language}">' in text
                assert title in text
                assert help_text in text
                assert f'data-spec-url="/docs/openapi.json?lang={language}"' in text
                assert f'/docs/asyncapi.json?lang={language}' in text
                assert "<script>" not in text
                assert i18n.current_language() == "en"

            for path in ("/docs/", "/docs/index.html"):
                status, _, body = await _request(service, f"{path}?lang=en")
                assert status == 200
                assert b'<html lang="en">' in body

            status, _, body = await _request(service, "/docs?lang=unknown")
            assert status == 200
            assert b'<html lang="en">' in body

            i18n.set_language("es")
            status, _, body = await _request(service, "/docs")
            assert status == 200
            assert b'<html lang="es">' in body
            assert i18n.current_language() == "es"
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_documentation_assets_and_unknown_routes() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            for name, mime in (
                ("docs.js", "text/javascript; charset=utf-8"),
                ("docs.css", "text/css; charset=utf-8"),
                ("swagger-ui-bundle.js", "text/javascript; charset=utf-8"),
                ("swagger-ui.css", "text/css; charset=utf-8"),
            ):
                status, headers, body = await _request(service, f"/docs/assets/{name}")
                assert status == 200
                assert headers["content-type"] == mime
                assert headers["cache-control"] == "no-cache"
                assert headers["x-content-type-options"] == "nosniff"
                assert body

            for path in ("/docs/assets/../rest.py", "/docs/assets/unknown.js", "/docs/unknown"):
                status, _, _ = await _request(service, path, bearer=True)
                assert status == 404
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_localized_spec_endpoints() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            documents = {}
            for family in ("openapi", "asyncapi"):
                for language in ("en", "es"):
                    status, headers, body = await _request(
                        service, f"/docs/{family}.json?lang={language}"
                    )
                    assert status == 200
                    assert headers["content-type"] == "application/json; charset=utf-8"
                    assert headers["content-language"] == language
                    assert headers["cache-control"] == "no-cache"
                    documents[(family, language)] = json.loads(body.decode("utf-8"))

            assert documents[("openapi", "en")]["info"] != documents[("openapi", "es")]["info"]
            assert documents[("asyncapi", "en")]["info"] != documents[("asyncapi", "es")]["info"]
            assert "/api/v1/skill.md" in documents[("openapi", "en")]["paths"]
            assert "/api/v1/ws" in documents[("openapi", "es")]["paths"]
            for language in ("en", "es"):
                asyncapi = documents[("asyncapi", language)]
                assert asyncapi["channels"]
                assert asyncapi["operations"]
                assert asyncapi["components"]["messages"]
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_skill_authentication_absence_and_exact_download(monkeypatch) -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            status, _, _ = await _request(service, "/api/v1/skill.md")
            assert status == 401

            def missing() -> str:
                raise FileNotFoundError

            monkeypatch.setattr(apidocs, "skill_text", missing)
            status, headers, body = await _request(service, "/api/v1/skill.md", bearer=True)
            assert status == 404
            assert json.loads(body) == {"error": "skill.md is not available"}
            assert headers["content-type"] == "application/json"

            markdown = "# API de GRAFENO\n\nDescripción con acentos: documentación.\n"
            monkeypatch.setattr(apidocs, "skill_text", lambda: markdown)
            for auth in ({"bearer": True}, {"token": "t1"}):
                status, headers, body = await _request(service, "/api/v1/skill.md", **auth)
                assert status == 200
                assert body == markdown.encode("utf-8")
                assert headers["content-type"] == "text/markdown; charset=utf-8"
                assert headers["content-disposition"] == 'attachment; filename="skill.md"'
                assert headers["cache-control"] == "no-store"
                assert headers["x-content-type-options"] == "nosniff"
        finally:
            _stop(service, task)

    asyncio.run(scenario())


def test_skill_text_uses_packaged_resource_path(tmp_path, monkeypatch) -> None:
    static = tmp_path / "static"
    static.mkdir()
    expected = "# Recurso empaquetado\n"
    (static / "skill.md").write_text(expected, encoding="utf-8")
    monkeypatch.setattr(apidocs.resources, "files", lambda package: tmp_path)
    assert apidocs.skill_text() == expected


def test_documentation_resources_are_packaged() -> None:
    root = resources.files("grafeno.server")
    names = (
        "static/docs.html",
        "static/docs.css",
        "static/docs.js",
        "static/swagger-ui/swagger-ui.css",
        "static/swagger-ui/swagger-ui-bundle.js",
        "static/swagger-ui/LICENSE.txt",
        "static/swagger-ui/VERSION.txt",
        "static/docs/openapi.en.json",
        "static/docs/openapi.es.json",
        "static/docs/asyncapi.en.json",
        "static/docs/asyncapi.es.json",
        "static/skill.md",
    )
    for name in names:
        assert root.joinpath(name).is_file(), name


def test_packaged_skill_contract() -> None:
    text = apidocs.skill_text()
    assert text.startswith("---\n")
    parts = text.split("---", 2)
    assert len(parts) == 3
    assert parts[0] == ""
    metadata = yaml.safe_load(parts[1])
    assert metadata["name"] == "grafeno-api"
    assert metadata["description"]
    body = parts[2]
    required = (
        "/api/v1/status",
        "/api/v1/tasks",
        "/api/v1/sessions",
        "/api/v1/ws",
        "/docs/openapi.json?lang=en",
        "/docs/asyncapi.json?lang=en",
        "Authorization: Bearer",
        "task.task",
        "SKILL.md",
        "grafeno-api",
        ".opencode/skills",
        ".claude/skills",
        ".agents/skills",
        "draft",
        "first_step",
        "planning",
        "planned",
        "implementing",
        "implemented",
        "reviewing",
        "fixing",
        "finalizing",
        "done",
        "failed",
        "paused",
        "discarded",
        "plan",
        "implement",
        "review",
        "fix",
        "final",
        "tests",
        "automode",
    )
    for value in required:
        assert value in body, value
    for marker in ("TODO", "TBD", "not installed yet", "Bearer t1"):
        assert marker not in text, marker


def test_packaged_skill_http_download() -> None:
    async def scenario():
        service, task = await _start_service()
        try:
            status, headers, body = await _request(service, "/api/v1/skill.md", bearer=True)
            assert status == 200
            assert body == apidocs.skill_text().encode("utf-8")
            assert headers["content-type"] == "text/markdown; charset=utf-8"
            assert headers["content-disposition"] == 'attachment; filename="skill.md"'
            assert headers["cache-control"] == "no-store"
            assert headers["x-content-type-options"] == "nosniff"
        finally:
            _stop(service, task)

    asyncio.run(scenario())
