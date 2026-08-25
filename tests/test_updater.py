"""Tests of the CLI auto-updater (no real CLIs executed)."""

from __future__ import annotations

import asyncio

from grafeno import updater
from grafeno.drivers.base import CLIDriver


class _FakeDriver(CLIDriver):
    name = "fake"
    display_name = "Fake CLI"
    executable = "python3"  # guaranteed present in the test env

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

    ok_cmd = ["python3", "-c", "import sys; sys.exit(0)"]
    ko_cmd = ["python3", "-c", "import sys; sys.exit(3)"]
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

    registry._DRIVERS["fake"] = _FakeDriver(["python3", "-c", "pass"])
    try:
        outcomes = asyncio.run(updater.update_all(["fake"]))
    finally:
        registry._DRIVERS.pop("fake", None)
    assert [o.cli for o in outcomes] == ["fake"]
    assert outcomes[0].ok
