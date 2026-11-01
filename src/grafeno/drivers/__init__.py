"""Registry of CLI drivers available in GRAFENO."""

from __future__ import annotations

import asyncio
from typing import Iterable

from .base import CLIDriver, EventKind, RunEvent, RunRequest, RunResult, model_levels
from .claude import ClaudeDriver
from .codex import CodexDriver
from .cursor import CursorDriver
from .kimi import KimiDriver
from .minimax import MiniMaxDriver
from .opencode import OpenCodeDriver

__all__ = [
    "CLIDriver",
    "EventKind",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "fetch_all_models",
    "fetch_all_variants",
    "get_driver",
    "model_levels",
    "available_clis",
]

_DRIVERS: dict[str, CLIDriver] = {
    driver.name: driver
    for driver in (
        OpenCodeDriver(), KimiDriver(), CodexDriver(), CursorDriver(), ClaudeDriver(),
        MiniMaxDriver(),
    )
}


def get_driver(name: str) -> CLIDriver:
    if name in _DRIVERS:
        return _DRIVERS[name]
    raise KeyError(f"CLI desconocido: '{name}'. Disponibles: {', '.join(_DRIVERS)}")


def available_clis() -> list[str]:
    """Supported CLIs whose executable is present on the system."""
    return [name for name, driver in _DRIVERS.items() if driver.is_available()]


async def fetch_all_models(clis: Iterable[str]) -> dict[str, list[str]]:
    """List the models of each CLI in parallel; a failing CLI returns an empty list.

    Cancelable: if the coroutine is cancelled, cancellation propagates to the
    in-flight ``list_models_async``, which kills the CLI's subprocess.
    """
    names = list(clis)

    async def one(cli: str) -> list[str]:
        try:
            return await get_driver(cli).list_models_async()
        except (KeyError, NotImplementedError, OSError):
            return []

    return dict(zip(names, await asyncio.gather(*(one(cli) for cli in names))))


async def fetch_all_variants(clis: Iterable[str]) -> dict[str, dict[str, list[str]]]:
    """Effort variants per model for each CLI, in parallel; a failing CLI
    returns an empty dict. Cancelable just like ``fetch_all_models``."""
    names = list(clis)

    async def one(cli: str) -> dict[str, list[str]]:
        try:
            return await get_driver(cli).list_variants_async()
        except (KeyError, NotImplementedError, OSError):
            return {}

    return dict(zip(names, await asyncio.gather(*(one(cli) for cli in names))))
