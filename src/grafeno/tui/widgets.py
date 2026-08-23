"""Widgets reutilizables de la TUI de GRAFENO."""

from __future__ import annotations

import inspect
from rich.text import Text
from textual.widgets import Markdown, Static

from ..models import STATE_LABELS, TaskState
from ..timefmt import format_duration

__all__ = ["PhaseBar", "markdown_set", "format_duration"]

_PHASE_ORDER = (
    ("plan", "Plan"),
    ("implement", "Implementación"),
    ("review", "Revisión"),
    ("done", "Fin"),
)


def _phase_status(state: TaskState) -> dict[str, str]:
    """Estado visual de cada fase: pending | active | done."""
    status = {"plan": "pending", "implement": "pending", "review": "pending", "done": "pending"}
    mapping = {
        TaskState.DRAFT: {},
        TaskState.PLANNING: {"plan": "active"},
        TaskState.PLANNED: {"plan": "done"},
        TaskState.IMPLEMENTING: {"plan": "done", "implement": "active"},
        TaskState.IMPLEMENTED: {"plan": "done", "implement": "done"},
        TaskState.REVIEWING: {"plan": "done", "implement": "done", "review": "active"},
        TaskState.FIXING: {"plan": "done", "implement": "active", "review": "done"},
        TaskState.DONE: {"plan": "done", "implement": "done", "review": "done", "done": "done"},
        TaskState.FAILED: {},
        TaskState.PAUSED: {},
    }
    status.update(mapping.get(state, {}))
    if state is TaskState.FAILED:
        for key, value in list(status.items()):
            if value == "pending":
                status[key] = "active"
                break
    return status


class PhaseBar(Static):
    """Barra de progreso del pipeline: Plan → Implementación → Revisión → Fin."""

    def __init__(self, state: TaskState = TaskState.DRAFT, iteration: int = 0, **kwargs):
        super().__init__(**kwargs)
        self._state = state
        self._iteration = iteration

    def on_mount(self) -> None:
        self._render_bar()

    def set_state(self, state: TaskState, iteration: int = 0) -> None:
        self._state = state
        self._iteration = iteration
        self._render_bar()

    def _render_bar(self) -> None:
        status = _phase_status(self._state)
        line = Text()
        for index, (key, label) in enumerate(_PHASE_ORDER):
            value = status[key]
            icon, style = {"pending": ("○", "dim"), "active": ("◉", "bold yellow"), "done": ("●", "green")}[value]
            if key == "review" and self._iteration > 0:
                label = f"{label} ×{self._iteration}"
            line.append(f" {icon} ", style=style)
            line.append(label, style=style if value != "pending" else "dim")
            if index < len(_PHASE_ORDER) - 1:
                line.append(" ─── ", style="dim")
        line.append(f"\n Estado: {STATE_LABELS.get(self._state, self._state.value)}", style="italic dim")
        self.update(line)


async def markdown_set(widget: Markdown, text: str) -> None:
    """Actualiza un widget Markdown (compatible con versiones sync/async)."""
    result = widget.update(text)
    if inspect.isawaitable(result):
        await result
