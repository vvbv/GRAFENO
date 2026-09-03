"""Best-effort auto-update of the supported agent CLIs.

Runs each CLI's native self-update command (``CLIDriver.update_command()``)
when the user enables ``auto_update`` in the global config. It NEVER raises:
a CLI that fails or has no update command is simply reported in the result.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

from .drivers import available_clis, get_driver

UPDATE_TIMEOUT = 300.0  # seconds per CLI update command


@dataclass
class UpdateOutcome:
    """Result of a single CLI update attempt."""

    cli: str
    ok: bool
    skipped: bool = False  # True when the CLI has no native update command
    detail: str = ""       # short error/summary text (never secrets)


async def update_cli(name: str, timeout: float = UPDATE_TIMEOUT) -> UpdateOutcome:
    """Run the native update command of one CLI (best effort)."""
    try:
        driver = get_driver(name)
    except KeyError:
        return UpdateOutcome(cli=name, ok=False, detail="unknown CLI")
    command = driver.resolve_command(driver.update_command())
    if not command:
        return UpdateOutcome(cli=name, ok=True, skipped=True)
    if not driver.is_available():
        return UpdateOutcome(cli=name, ok=False, detail="not installed")
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return UpdateOutcome(cli=name, ok=False, detail=str(exc))
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return UpdateOutcome(cli=name, ok=False, detail="timeout")
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    tail = stdout.decode("utf-8", errors="replace").strip().splitlines()
    detail = tail[-1][:200] if tail else ""
    return UpdateOutcome(
        cli=name, ok=process.returncode == 0, detail=detail
    )


async def update_all(clis: Iterable[str] | None = None) -> list[UpdateOutcome]:
    """Update every installed CLI that has a native update command.

    Defaults to the currently installed CLIs (``available_clis()``).
    Cancelable: cancellation propagates to the in-flight subprocess.
    """
    names = list(clis) if clis is not None else available_clis()
    outcomes: list[UpdateOutcome] = []
    for name in names:
        outcomes.append(await update_cli(name))
    return outcomes
