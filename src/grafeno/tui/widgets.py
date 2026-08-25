"""Reusable widgets of the GRAFENO TUI."""

from __future__ import annotations

import inspect
from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.widgets import Header, Markdown, Static
from textual.widgets._header import HeaderIcon, HeaderTitle

from ..i18n import t
from ..models import TaskState, state_label
from ..timefmt import format_duration

__all__ = ["DateTimeClock", "GrafenoHeader", "PhaseBar", "markdown_set", "format_duration"]

_PHASE_ORDER = (
    ("plan", "phase.plan"),
    ("implement", "phase.implement"),
    ("review", "phase.review"),
    ("final", "phase.final"),
    ("done", "phase.end"),
)


def _phase_status(state: TaskState) -> dict[str, str]:
    """Visual state of each phase: pending | active | done."""
    status = {"plan": "pending", "implement": "pending", "review": "pending", "final": "pending", "done": "pending"}
    mapping = {
        TaskState.DRAFT: {},
        TaskState.PLANNING: {"plan": "active"},
        TaskState.PLANNED: {"plan": "done"},
        TaskState.IMPLEMENTING: {"plan": "done", "implement": "active"},
        TaskState.IMPLEMENTED: {"plan": "done", "implement": "done"},
        TaskState.REVIEWING: {"plan": "done", "implement": "done", "review": "active"},
        TaskState.FIXING: {"plan": "done", "implement": "active", "review": "done"},
        TaskState.FINALIZING: {"plan": "done", "implement": "done", "review": "done", "final": "active"},
        TaskState.DONE: {"plan": "done", "implement": "done", "review": "done", "final": "done", "done": "done"},
        TaskState.FAILED: {},
        TaskState.PAUSED: {},
        TaskState.DISCARDED: {},
    }
    status.update(mapping.get(state, {}))
    if state is TaskState.FAILED:
        for key, value in list(status.items()):
            if value == "pending":
                status[key] = "active"
                break
    return status


class PhaseBar(Static):
    """Pipeline progress bar: Plan -> Implementation -> Review -> Final steps -> End."""

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
        for index, (key, label_key) in enumerate(_PHASE_ORDER):
            label = t(label_key)
            value = status[key]
            icon, style = {"pending": ("○", "dim"), "active": ("◉", "bold yellow"), "done": ("●", "green")}[value]
            if key == "review" and self._iteration > 0:
                label = f"{label} ×{self._iteration}"
            line.append(f" {icon} ", style=style)
            line.append(label, style=style if value != "pending" else "dim")
            if index < len(_PHASE_ORDER) - 1:
                line.append(" ─── ", style="dim")
        line.append(t("phasebar.state", label=state_label(self._state)), style="italic dim")
        self.update(line)


async def markdown_set(widget: Markdown, text: str) -> None:
    """Update a Markdown widget (compatible with sync/async versions)."""
    result = widget.update(text)
    if inspect.isawaitable(result):
        await result


class DateTimeClock(Static):
    """Clock with date and time (seconds included) for the app header."""

    def on_mount(self) -> None:
        self._update_clock()
        self.set_interval(1.0, self._update_clock)

    def _update_clock(self) -> None:
        self.update(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


class GrafenoHeader(Header):
    """App header that always shows the current date and time on the right."""

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield DateTimeClock(id="clock")
