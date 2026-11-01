"""Tests of the CLI quota API (GET /api/v1/quota, WS ``quota.get``)."""

from __future__ import annotations

import asyncio
import json

from grafeno import quota
from grafeno.server import cliquota, rest, ws
from grafeno.server.httpcore import Request


class FakeService:
    app = None

    def _log(self, message: str) -> None:
        pass


def _service() -> tuple[FakeService, list[str]]:
    calls: list[str] = []

    async def fetch(shell):
        calls.append(shell.cli)
        return quota.CliQuota(
            shell.cli, quota.STATUS_OK, "api", [quota.QuotaWindow("five_hour", 10.0)],
            fetched_at="t",
        )

    service = FakeService()
    service._quota_monitor = quota.QuotaMonitor(
        collect=lambda: [quota.CliQuota("claude")], fetch=fetch,
    )
    return service, calls


def _get(service, query: dict[str, str] | None = None):
    request = Request("GET", "/api/v1/quota", query or {}, "HTTP/1.1", {}, b"")
    response, _ = asyncio.run(rest.dispatch_rest(service, request))
    return response


def test_rest_live_then_throttled() -> None:
    service, calls = _service()
    first = _get(service)
    assert first.status == 200
    body = json.loads(first.body)
    assert body["throttled"] is False and body["clis"][0]["cli"] == "claude"
    assert "instance_name" in body
    second = _get(service)
    assert second.status == 200
    assert json.loads(second.body)["throttled"] is True
    assert int(second.headers["Retry-After"]) >= 1
    assert len(calls) == 1


def test_rest_cached() -> None:
    service, _ = _service()
    response = _get(service, {"cached": "1"})
    assert json.loads(response.body)["mode"] == "cached"
    assert "Retry-After" not in response.headers


def test_ws_cached() -> None:
    service, _ = _service()
    result = asyncio.run(ws.METHODS["quota.get"](service, {"cached": True}, None))
    assert result["mode"] == "cached"


def test_monitor_from_app() -> None:
    class App:
        quota_monitor = quota.QuotaMonitor()

    service = FakeService()
    service.app = App()
    assert cliquota.monitor(service) is service.app.quota_monitor
