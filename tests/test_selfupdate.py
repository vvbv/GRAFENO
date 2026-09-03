"""Tests of the self-update module (no network, no real pip/pipx)."""

from __future__ import annotations

import asyncio
import urllib.error

from grafeno import selfupdate


def test_normalize_version():
    assert selfupdate.normalize_version("v1.42.0") == "1.42.0"
    assert selfupdate.normalize_version("  V2.0.0 ") == "2.0.0"
    assert selfupdate.normalize_version("1.41.0") == "1.41.0"


def test_parse_version_drops_suffixes():
    assert selfupdate.parse_version("v1.42.0") == (1, 42, 0)
    assert selfupdate.parse_version("1.42.0-beta.1") == (1, 42, 0)
    assert selfupdate.parse_version("1.42+build5") == (1, 42)
    assert selfupdate.parse_version("") == (0,)


def test_is_newer():
    assert selfupdate.is_newer("v1.42.0", "1.41.0")
    assert not selfupdate.is_newer("1.41.0", "1.41.0")
    assert not selfupdate.is_newer("1.40.9", "1.41.0")
    assert selfupdate.is_newer("2.0", "1.99.99")


def test_fetch_latest_version_ok():
    def fake_opener(request, timeout):
        assert request.headers["User-agent"] == selfupdate.USER_AGENT
        return b'{"tag_name": "v9.9.9"}'

    latest = asyncio.run(selfupdate.fetch_latest_version(opener=fake_opener))
    assert latest == "9.9.9"


def test_fetch_latest_version_network_error():
    def bad_opener(request, timeout):
        raise urllib.error.URLError("boom")

    assert asyncio.run(selfupdate.fetch_latest_version(opener=bad_opener)) is None


def test_fetch_latest_version_bad_json():
    assert asyncio.run(
        selfupdate.fetch_latest_version(opener=lambda req, t: b"not json")
    ) is None
    assert asyncio.run(
        selfupdate.fetch_latest_version(opener=lambda req, t: b"{}")
    ) is None


def test_build_update_command_prefers_pipx(monkeypatch):
    monkeypatch.setattr(
        selfupdate.shutil, "which", lambda name: "/usr/local/bin/pipx"
    )
    cmd = selfupdate.build_update_command("1.42.0")
    assert cmd[:3] == ["/usr/local/bin/pipx", "install", "--force"]
    assert cmd[3] == "git+https://github.com/vvbv/GRAFENO.git@v1.42.0"


def test_build_update_command_pip_fallback(monkeypatch):
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: None)
    cmd = selfupdate.build_update_command("v1.42.0")
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "--upgrade" in cmd
    assert cmd[-1].endswith("@v1.42.0")


def test_run_self_update_success_and_failure(monkeypatch):
    async def fake_ok(command, timeout):
        return 0, "installed grafeno 1.42.0"

    async def fake_ko(command, timeout):
        return 1, "pip exploded"

    monkeypatch.setattr(selfupdate, "_run_command", fake_ok)
    outcome = asyncio.run(selfupdate.run_self_update("v1.42.0"))
    assert outcome.ok and outcome.version == "1.42.0"
    monkeypatch.setattr(selfupdate, "_run_command", fake_ko)
    outcome = asyncio.run(selfupdate.run_self_update("1.42.0"))
    assert not outcome.ok and outcome.detail == "pip exploded"


def test_run_self_update_oserror(monkeypatch):
    async def fake_oserror(command, timeout):
        raise OSError("pipx not found")

    monkeypatch.setattr(selfupdate, "_run_command", fake_oserror)
    outcome = asyncio.run(selfupdate.run_self_update("1.42.0"))
    assert not outcome.ok and "pipx" in outcome.detail


def test_cli_update_already_latest(monkeypatch, capsys):
    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return "0.0.1"

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    assert selfupdate.cli_update() == 0
    assert "up to date" in capsys.readouterr().out


def test_cli_update_check_failed(monkeypatch, capsys):
    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return None

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    assert selfupdate.cli_update() == 1
    assert "Could not check" in capsys.readouterr().out


def test_cli_update_runs_and_reports(monkeypatch, capsys):
    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return "99.0.0"

    async def fake_run(version, timeout=selfupdate.UPDATE_TIMEOUT):
        return selfupdate.SelfUpdateOutcome(version=version, ok=True)

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    monkeypatch.setattr(selfupdate, "run_self_update", fake_run)
    assert selfupdate.cli_update() == 0
    assert "99.0.0" in capsys.readouterr().out


def test_cli_update_failure_exit_code(monkeypatch, capsys):
    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return "99.0.0"

    async def fake_run(version, timeout=selfupdate.UPDATE_TIMEOUT):
        return selfupdate.SelfUpdateOutcome(version=version, ok=False, detail="boom")

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    monkeypatch.setattr(selfupdate, "run_self_update", fake_run)
    assert selfupdate.cli_update() == 1
    assert "boom" in capsys.readouterr().out


def test_config_self_update_roundtrip():
    from grafeno import config as config_module

    cfg = config_module.load()
    assert cfg.self_update is False
    cfg.self_update = True
    config_module.save(cfg)
    assert config_module.load().self_update is True