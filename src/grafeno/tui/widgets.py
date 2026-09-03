"""Reusable widgets of the GRAFENO TUI."""

from __future__ import annotations

import inspect
import os
from datetime import datetime

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.widgets import Header, Markdown, Static, TextArea
from textual.widgets._header import HeaderIcon, HeaderTitle

from .. import media
from ..i18n import t
from ..models import Task, TaskState, state_label
from ..timefmt import format_duration

__all__ = [
    "DateTimeClock",
    "GrafenoHeader",
    "LocationBar",
    "MediaTextArea",
    "PhaseBar",
    "markdown_set",
    "format_duration",
]

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

    def __init__(
        self,
        state: TaskState = TaskState.DRAFT,
        iteration: int = 0,
        waiting: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._state = state
        self._iteration = iteration
        self._waiting = waiting

    def on_mount(self) -> None:
        self._render_bar()

    def set_state(self, state: TaskState, iteration: int = 0, waiting: bool = False) -> None:
        self._state = state
        self._iteration = iteration
        self._waiting = waiting
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
        label = state_label(self._state)
        if self._waiting:
            label = f"{label} {t('state.waiting')}"
        line.append(t("phasebar.state", label=label), style="italic dim")
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
    """App header: clock on the right and orange hint when an update exists."""

    def on_mount(self) -> None:
        self.watch(self.app, "available_update", self._refresh_title)

    def _refresh_title(self, *_args) -> None:
        self.query_one(HeaderTitle).update(self.format_title())

    def format_title(self) -> Content:
        """Title + subtitle plus ``(v X.Y.Z available)`` in orange."""
        content = super().format_title()
        latest = getattr(self.app, "available_update", "")
        if latest:
            content = Content.assemble(
                content,
                (" " + t("app.update_available", version=latest), "dark_orange"),
            )
        return content

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield DateTimeClock(id="clock")


class LocationBar(Static):
    """One-line bar with the current path and, optionally, the task path.

    Always shows the directory GRAFENO was launched from; when a task is
    given it also shows the task's project path, with an ``[SSH]`` badge
    when the task points to a remote host.
    """

    def __init__(self, task: Task | None = None, **kwargs):
        super().__init__(**kwargs)
        # NOTE: stored as ``_bar_task`` (not ``_task``) because
        # ``MessagePump._task`` is the asyncio task driving this widget's
        # message pump and would otherwise be overwritten.
        self._bar_task: Task | None = task

    def on_mount(self) -> None:
        self._render_bar()

    def set_task(self, task: Task) -> None:
        """Update the task shown in the bar (e.g. after remote OS probing)."""
        self._bar_task = task
        self._render_bar()

    def _render_bar(self) -> None:
        # Local import avoids cycles: widgets is imported early during boot.
        from .. import remotesession

        line = Text()
        if remotesession.active():
            line.append(t("loc.session", target=remotesession.label()), style="dim")
        else:
            line.append(t("loc.cwd", path=os.getcwd()), style="dim")
        if self._bar_task is not None:
            line.append("  ·  ", style="dim")
            remote_like = self._bar_task.is_remote or remotesession.active()
            display = self._bar_task.remote if self._bar_task.is_remote else self._bar_task.workdir
            line.append(t("loc.task", path=display), style="dim")
            if remote_like:
                line.append(f" {t('loc.remote')}", style="bold yellow")
        self.update(line)


class MediaTextArea(TextArea):
    """TextArea that stores pasted clipboard images under the task media dir.

    When the clipboard holds a PNG at paste time, the image is saved and a
    ``media/media-NN.png`` token is inserted at the cursor instead of text.
    Without a ``task_id`` (task not created yet) images are buffered in
    ``pending`` and must be flushed with :func:`media.save_pending`.
    """

    BINDINGS = [Binding("ctrl+v", "paste_media", show=False, priority=True)]

    def __init__(self, *args, task_id: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._media_task_id = task_id
        self.pending: list[tuple[str, bytes]] = []  # (suggested name, png bytes)

    async def _on_paste(self, event: events.Paste) -> None:
        if await self._try_paste_image():
            return
        await super()._on_paste(event)

    async def action_paste_media(self) -> None:
        if not await self._try_paste_image():
            self.action_paste()  # normal text paste fallback

    async def _try_paste_image(self) -> bool:
        data = await media.read_clipboard_image_async()
        if data is None:
            return False
        name = self._store_image(data)
        if name is None:
            return False
        self.insert(media.MEDIA_TOKEN_PREFIX + name)
        self.notify(t("media.pasted", name=name))
        return True

    def _store_image(self, data: bytes) -> str | None:
        """Persist ``data`` and return the final on-disk file name (or None)."""
        if self._media_task_id is None:
            name = media.next_pending_name(self.pending)
            self.pending.append((name, data))
            return name
        path = media.save_image(self._media_task_id, data)
        if path is None:
            return None
        return path.name
