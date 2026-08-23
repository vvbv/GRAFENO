"""Registro de drivers de CLI disponibles en GRAFENO."""

from __future__ import annotations

from .base import CLIDriver, EventKind, RunEvent, RunRequest, RunResult
from .kimi import KimiDriver
from .opencode import OpenCodeDriver

__all__ = [
    "CLIDriver",
    "EventKind",
    "RunEvent",
    "RunRequest",
    "RunResult",
    "get_driver",
    "available_clis",
]

_DRIVERS: dict[str, CLIDriver] = {
    driver.name: driver for driver in (OpenCodeDriver(), KimiDriver())
}

# CLIs previstos en la arquitectura pero aún no implementados.
PLANNED_CLIS = ("codex", "claude")


def get_driver(name: str) -> CLIDriver:
    if name in _DRIVERS:
        return _DRIVERS[name]
    if name in PLANNED_CLIS:
        raise NotImplementedError(
            f"El CLI '{name}' está previsto en la arquitectura pero aún no tiene driver."
        )
    raise KeyError(f"CLI desconocido: '{name}'. Disponibles: {', '.join(_DRIVERS)}")


def available_clis() -> list[str]:
    """CLIs soportados cuyo ejecutable existe en el sistema."""
    return [name for name, driver in _DRIVERS.items() if driver.is_available()]
