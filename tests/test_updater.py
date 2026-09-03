"""Tests of the CLI auto-updater (no real CLIs executed)."""

from __future__ import annotations

import asyncio
import sys

from grafeno import updater
from grafeno.drivers.base import CLIDriver


class _FakeDriver(CLIDriver):
    name = "fake"
    display_name = "Fake CLI"
    # sys.executable: on Windows ``python3`` is a broken Microsoft Store alias.
    executable = sys.executable

    def __init__(self, update_cmd: list[str], available: bool = True):
        self._update_cmd = update_cmd
        self._available = available

    def update_command(self) -> list[str]:
        return self._update_cmd

    def is_available(self) -> bool:
        return self._available


def test_update_cli_skipped_without_command():
    import grafeno.drivers as registry

    registry._DRIVERS["fake"] = _FakeDriver([])
    try:
        outcome = asyncio.run(updater.update_cli("fake"))
    finally:
        registry._DRIVERS.pop("fake", None)
    assert outcome.ok and outcome.skipped


def test_update_cli_not_installed():
    import grafeno.drivers as registry

    registry._DRIVERS["fake"] = _FakeDriver(["true"], available=False)
    try:
        outcome = asyncio.run(updater.update_cli("fake"))
    finally:
        registry._DRIVERS.pop("fake", None)
    assert not outcome.ok and not outcome.skipped


def test_update_cli_success_and_failure():
    import grafeno.drivers as registry

    ok_cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    ko_cmd = [sys.executable, "-c", "import sys; sys.exit(3)"]
    registry._DRIVERS["fake"] = _FakeDriver(ok_cmd)
    try:
        assert asyncio.run(updater.update_cli("fake")).ok
        registry._DRIVERS["fake"] = _FakeDriver(ko_cmd)
        outcome = asyncio.run(updater.update_cli("fake"))
        assert not outcome.ok and not outcome.skipped
    finally:
        registry._DRIVERS.pop("fake", None)


def test_update_all_filters_and_collects():
    import grafeno.drivers as registry

    registry._DRIVERS["fake"] = _FakeDriver([sys.executable, "-c", "pass"])
    try:
        outcomes = asyncio.run(updater.update_all(["fake"]))
    finally:
        registry._DRIVERS.pop("fake", None)
    assert [o.cli for o in outcomes] == ["fake"]
    assert outcomes[0].ok


def test_update_command_resolves_executable(monkeypatch):
    """The update command spawns the resolved executable path (Windows .cmd)."""
    import grafeno.drivers as registry

    monkeypatch.setattr(
        "grafeno.drivers.base.shutil.which",
        lambda name: f"C:/shims/{name}.cmd",
    )
    registry._DRIVERS["fake"] = _FakeDriver(["fake", "update"])
    seen = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return (b"", b"")

    async def _fake_exec(*cmd, **kwargs):
        seen.extend(cmd)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    try:
        outcome = asyncio.run(updater.update_cli("fake"))
    finally:
        registry._DRIVERS.pop("fake", None)
    assert outcome.ok
    assert seen[0] == "C:/shims/fake.cmd"
