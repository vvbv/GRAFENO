"""Tests for the CLI quota module (providers, parsers and monitor)."""

from __future__ import annotations

import asyncio
import io
import json
import time
import urllib.error

from grafeno import quota
from grafeno.config import Config, RoleConfig
from grafeno.profiles import Profile

CLAUDE_USAGE = {
    "five_hour": {"utilization": 2.0, "resets_at": "2026-11-01T09:49:59.609014+00:00"},
    "seven_day": {"utilization": 40.0, "resets_at": "2026-11-04T21:59:59.609030+00:00"},
    "seven_day_opus": None,
    "iguana_necktie": {"utilization": 99.0},
    "extra_usage": {"is_enabled": True, "utilization": 1.52, "monthly_limit": 14500},
}


def _write_claude_creds(expires_ms: float) -> None:
    path = quota.claude_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_ms}}))


def _ok_opener(payload):
    def opener(request, timeout):
        return json.dumps(payload).encode()
    return opener


def test_configured_clis(monkeypatch):
    cfg = Config()
    for name in ("planner", "implementer", "reviewer", "final"):
        setattr(cfg, name, RoleConfig(cli="claude"))
    cfg.first = RoleConfig(cli="codex")
    cfg.final = RoleConfig(cli="")
    cfg.first_prompt = ""
    prof_a = Profile(name="a", planner=RoleConfig(cli="kimi"), first=RoleConfig(cli="cursor"))
    prof_b = Profile(name="b", planner=RoleConfig(cli="kimi"))
    monkeypatch.setattr(quota, "get_driver", lambda cli: type(
        "D", (), {"is_available": lambda self: cli != "kimi"})())
    result = quota.configured_clis(cfg, [prof_a, prof_b])
    names = [c.cli for c in result]
    assert names.index("kimi") < names.index("claude")  # KNOWN_CLIS order
    assert "codex" not in names and "cursor" not in names  # first step disabled
    by = {c.cli: c for c in result}
    assert by["claude"].used_by_default and not by["claude"].profiles
    assert by["kimi"].profiles == ["a", "b"] and not by["kimi"].used_by_default
    assert by["kimi"].installed is False and by["claude"].installed is True
    cfg.first_prompt = "x"
    enabled = [c.cli for c in quota.configured_clis(cfg, [prof_a])]
    assert "codex" in enabled and "cursor" in enabled


def test_parse_claude_usage():
    windows = {w.key: w for w in quota.parse_claude_usage(CLAUDE_USAGE)}
    assert set(windows) == {"five_hour", "seven_day", "extra_usage"}
    assert windows["five_hour"].used_percent == 2.0
    d = windows["seven_day"].to_dict()
    assert d["remaining_percent"] == 60.0 and d["resets_in_seconds"] is not None


def test_fetch_claude(monkeypatch):
    assert quota._fetch_claude(quota.default_opener).status == quota.STATUS_NO_CREDENTIALS
    _write_claude_creds((time.time() - 100) * 1000)
    assert quota._fetch_claude(quota.default_opener).status == quota.STATUS_AUTH_EXPIRED
    _write_claude_creds((time.time() + 3600) * 1000)

    def denied(request, timeout):
        raise urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b""))

    monkeypatch.setattr(quota, "default_opener", denied)
    assert quota._fetch_claude(denied).status == quota.STATUS_AUTH_EXPIRED
    monkeypatch.setattr(quota, "default_opener", _ok_opener(CLAUDE_USAGE))
    res = quota._fetch_claude(quota.default_opener)
    assert res.status == quota.STATUS_OK and res.source == "api"
    quota.observe_claude_rate_limit({"unifiedWindows": {
        "five_hour": {"utilization": 0.31, "resetsAt": 1789248600}}})
    monkeypatch.setattr(quota, "default_opener", denied)
    res = quota._fetch_claude(denied)
    assert res.status == quota.STATUS_OK and res.source == "stream"
    assert res.message == "api: auth_expired"


def test_observe_claude_rate_limit():
    quota.observe_claude_rate_limit({"unifiedWindows": {
        "five_hour": {"utilization": 0.31, "resetsAt": 1789248600},
        "seven_day": {"utilization": 0.03, "resetsAt": 1789596000}}})
    seen = quota.observed("claude")
    assert {w.key: round(w.used_percent, 1) for w in seen.windows} == {
        "five_hour": 31.0, "seven_day": 3.0}


def test_kimi(monkeypatch):
    creds = quota.kimi_home() / "credentials"
    creds.mkdir(parents=True)
    (creds / "a.json").write_text(json.dumps(
        {"access_token": "t", "expires_at": time.time() - 10}))

    def boom(request, timeout):
        raise AssertionError("must not be called")

    monkeypatch.setattr(quota, "default_opener", boom)
    assert quota._fetch_kimi(boom).status == quota.STATUS_AUTH_EXPIRED
    (creds / "a.json").write_text(json.dumps(
        {"access_token": "t", "expires_at": time.time() + 3600}))
    payload = {"usages": {
        "limit_5h": {"used_ratio": 0.12, "reset_time": "2026-11-01T10:00:00Z"},
        "limit_7d": {"used_ratio": "0.5"},
        "limit_month_total": {"used_ratio": 0.2}}}
    monkeypatch.setattr(quota, "default_opener", _ok_opener(payload))
    res = quota._fetch_kimi(quota.default_opener)
    assert res.status == quota.STATUS_OK
    assert [w.key for w in res.windows] == ["five_hour", "seven_day", "month_total"]
    assert res.windows[0].used_percent == 12.0


def test_codex():
    assert quota._fetch_codex(quota.default_opener).status == quota.STATUS_NO_DATA
    folder = quota.codex_home() / "sessions" / "2026" / "11" / "01"
    folder.mkdir(parents=True)
    lines = [
        {"timestamp": "2026-11-01T10:00:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1761000000},
                "secondary": {"used_percent": 40.0, "window_minutes": 10080,
                              "resets_in_seconds": 3600}}}},
    ]
    (folder / "rollout-x.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    res = quota._fetch_codex(quota.default_opener)
    assert res.status == quota.STATUS_OK and res.source == "rollout"
    assert [w.key for w in res.windows] == ["five_hour", "seven_day"]
    assert res.windows[1].resets_at.startswith("2026-11-01T11:00:00")


def test_fetch_cli():
    gone = asyncio.run(quota.fetch_cli(quota.CliQuota("claude", installed=False)))
    assert gone.status == quota.STATUS_NOT_INSTALLED
    other = asyncio.run(quota.fetch_cli(quota.CliQuota("opencode")))
    assert other.status == quota.STATUS_UNSUPPORTED


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _monitor(clock, results):
    calls = []

    def collect():
        return [quota.CliQuota("claude")]

    async def fetch(shell):
        calls.append(shell.cli)
        await asyncio.sleep(0.01)
        return results.pop(0) if len(results) > 1 else results[0]

    return quota.QuotaMonitor(collect=collect, fetch=fetch, clock=clock), calls


def _good():
    return quota.CliQuota("claude", quota.STATUS_OK, "api", [quota.QuotaWindow("five_hour", 10)],
                          fetched_at="t")


def test_monitor_throttling_and_cache():
    clock = _Clock()
    monitor, calls = _monitor(clock, [_good()])

    async def run():
        first = await monitor.get(live=True)
        second = await monitor.get(live=True)
        assert not first["throttled"] and second["throttled"]
        assert second["retry_after"] > 0 and "Retry in" in second["warning"]
        assert len(calls) == 1
        clock.now += 61
        await monitor.get(live=True)
        assert len(calls) == 2
        cached = await monitor.get(live=False)
        assert cached["mode"] == "cached" and len(calls) == 2
        clock.now += 301
        await monitor.get(live=False)
        assert len(calls) == 3

    asyncio.run(run())


def test_monitor_stale_and_single_flight():
    clock = _Clock()
    bad = quota.CliQuota("claude", quota.STATUS_ERROR, message="OSError")
    monitor, calls = _monitor(clock, [_good(), bad])

    async def run():
        await monitor.refresh()
        clock.now += 10
        payload = await monitor.refresh()
        item = payload["clis"][0]
        assert item["stale"] is True and item["status"] == quota.STATUS_ERROR
        assert item["windows"][0]["key"] == "five_hour"

    asyncio.run(run())

    clock2 = _Clock()
    monitor2, calls2 = _monitor(clock2, [_good()])

    async def concurrent():
        await asyncio.gather(monitor2.refresh(), monitor2.refresh())

    asyncio.run(concurrent())
    assert len(calls2) == 1
