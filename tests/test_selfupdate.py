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


def test_installed_via_pipx_detection(monkeypatch):
    """Interpreter heuristic: a ``pipx`` path component anywhere in sys.prefix."""
    monkeypatch.setattr(selfupdate.sys, "prefix", "/home/u/.local/share/pipx/venvs/grafeno")
    assert selfupdate.installed_via_pipx()
    monkeypatch.setattr(selfupdate.sys, "prefix", "C:\\Users\\u\\pipx\\venvs\\grafeno")
    assert selfupdate.installed_via_pipx()
    monkeypatch.setattr(selfupdate.sys, "prefix", "/usr")
    assert not selfupdate.installed_via_pipx()


def test_build_update_command_prefers_pipx(monkeypatch):
    monkeypatch.setattr(selfupdate, "installed_via_pipx", lambda: True)
    monkeypatch.setattr(
        selfupdate.shutil, "which", lambda name: "/usr/local/bin/pipx"
    )
    cmd = selfupdate.build_update_command("1.42.0")
    assert cmd[:3] == ["/usr/local/bin/pipx", "install", "--force"]
    assert cmd[-1] == "git+https://github.com/vvbv/GRAFENO.git@v1.42.0"
    assert "--pip-args" in cmd and "--progress-bar=on" in cmd


def test_build_update_command_pip_fallback(monkeypatch):
    monkeypatch.setattr(selfupdate, "installed_via_pipx", lambda: False)
    cmd = selfupdate.build_update_command("v1.42.0")
    assert cmd[1:4] == ["-m", "pip", "install"]
    assert "--upgrade" in cmd
    assert "--force-reinstall" in cmd
    assert "--progress-bar=on" in cmd
    assert cmd[-1].endswith("@v1.42.0")


def test_build_update_command_ignores_pipx_when_installed_with_pip(monkeypatch):
    """A pipx binary on PATH is ignored when GRAFENO was installed with pip."""
    monkeypatch.setattr(selfupdate, "installed_via_pipx", lambda: False)
    monkeypatch.setattr(selfupdate.shutil, "which", lambda name: "/usr/bin/pipx")
    cmd = selfupdate.build_update_command("1.42.0")
    assert cmd[0] != "/usr/bin/pipx"
    assert "--force-reinstall" in cmd


def test_run_self_update_success_and_failure(monkeypatch):
    async def fake_ok(command, timeout, on_chunk=None):
        return 0, "installed grafeno 1.42.0"

    async def fake_ko(command, timeout, on_chunk=None):
        return 1, "pip exploded"

    monkeypatch.setattr(selfupdate, "_run_command", fake_ok)
    monkeypatch.setattr(selfupdate, "installed_version", lambda: "1.42.0")
    outcome = asyncio.run(selfupdate.run_self_update("v1.42.0"))
    assert outcome.ok and outcome.version == "1.42.0"
    monkeypatch.setattr(selfupdate, "_run_command", fake_ko)
    outcome = asyncio.run(selfupdate.run_self_update("1.42.0"))
    assert not outcome.ok and outcome.detail == "pip exploded"


def test_run_self_update_fails_on_version_mismatch(monkeypatch):
    """Exit code 0 without the target version installed is a failed update."""
    async def fake_ok(command, timeout, on_chunk=None):
        return 0, "done"

    monkeypatch.setattr(selfupdate, "_run_command", fake_ok)
    monkeypatch.setattr(selfupdate, "installed_version", lambda: "1.41.0")
    outcome = asyncio.run(selfupdate.run_self_update("1.42.0"))
    assert not outcome.ok
    assert "1.41.0" in outcome.detail


def test_run_self_update_ok_when_install_unqueryable(monkeypatch):
    """When the installed version cannot be checked, the exit code is trusted."""
    async def fake_ok(command, timeout, on_chunk=None):
        return 0, "done"

    monkeypatch.setattr(selfupdate, "_run_command", fake_ok)
    monkeypatch.setattr(selfupdate, "installed_version", lambda: None)
    assert asyncio.run(selfupdate.run_self_update("1.42.0")).ok


def test_run_self_update_oserror(monkeypatch):
    async def fake_oserror(command, timeout, on_chunk=None):
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

    async def fake_run(version, timeout=selfupdate.UPDATE_TIMEOUT, on_progress=None):
        return selfupdate.SelfUpdateOutcome(version=version, ok=True)

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    monkeypatch.setattr(selfupdate, "run_self_update", fake_run)
    assert selfupdate.cli_update() == 0
    assert "99.0.0" in capsys.readouterr().out


def test_cli_update_failure_exit_code(monkeypatch, capsys):
    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return "99.0.0"

    async def fake_run(version, timeout=selfupdate.UPDATE_TIMEOUT, on_progress=None):
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


def test_extract_percent():
    assert selfupdate.extract_percent("Cloning into repo...") is None
    assert selfupdate.extract_percent(" |#| 42 %") == 42.0
    assert selfupdate.extract_percent("a 10 %\rb 99.5 %") == 99.5


def test_render_progress():
    bar = selfupdate.render_progress(50)
    assert bar.startswith("[") and bar.endswith("] 50%")
    inner = bar[bar.index("[") + 1 : bar.index("]")]
    assert len(inner) == 30
    assert set(inner) <= {"#", "-"}
    assert selfupdate.render_progress(140).endswith("] 100%")
    assert selfupdate.render_progress(-3).endswith("] 0%")


def test_terminal_progress_feed_writes_bar(capsys):
    progress = selfupdate.TerminalProgress()
    progress.feed("Downloading dep\r |##| 25 %\r")
    progress.feed("noise without markers")
    out = capsys.readouterr().out
    assert progress.written
    assert "25%" in out


def test_cli_update_announces_target_version(monkeypatch, capsys):
    """The update command prints current -> latest BEFORE installing."""
    from grafeno import __version__

    async def fake_fetch(timeout=selfupdate.CHECK_TIMEOUT, opener=None):
        return "99.0.0"

    async def fake_run(version, timeout=selfupdate.UPDATE_TIMEOUT, on_progress=None):
        return selfupdate.SelfUpdateOutcome(version=version, ok=True)

    monkeypatch.setattr(selfupdate, "fetch_latest_version", fake_fetch)
    monkeypatch.setattr(selfupdate, "run_self_update", fake_run)
    assert selfupdate.cli_update() == 0
    out = capsys.readouterr().out
    assert "99.0.0" in out and __version__ in out
    assert "Updating GRAFENO" in out or "Actualizando GRAFENO" in out
    lines = out.splitlines()
    announce = [i for i, ln in enumerate(lines) if "99.0.0" in ln and "updated" not in ln]
    assert announce  # the announcement line exists before the final result


def test_run_self_update_forwards_progress(monkeypatch):
    seen = []

    async def fake(command, timeout, on_chunk=None):
        seen.append(on_chunk)
        return 0, "ok"

    monkeypatch.setattr(selfupdate, "_run_command", fake)
    monkeypatch.setattr(selfupdate, "installed_version", lambda: "1.42.0")

    def probe(text):
        pass

    asyncio.run(selfupdate.run_self_update("1.42.0", on_progress=probe))
    assert seen and seen[0] is probe