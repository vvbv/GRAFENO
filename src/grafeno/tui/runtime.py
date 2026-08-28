"""Background task runtimes.

One ``TaskRuntime`` per task, registered on the App (not the screen): the
pipeline worker survives screen navigation and several tasks can run in
parallel. Screens subscribe as listeners to paint the log and state, and
unsubscribe on exit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from rich.text import Text
from textual.worker import Worker

from .. import live_log, models
from ..drivers.base import EventKind, RunEvent
from ..i18n import t
from ..models import Task, TaskState
from ..pipeline.orchestrator import Orchestrator

Runner = Callable[[Orchestrator], Awaitable[None]]
Listener = Callable[[str, object], None]  # (kind, payload): "log" | "state"

_MAX_LOG_ENTRIES = 5000


def _default_orchestrator(task: Task, **callbacks) -> Orchestrator:
    return Orchestrator(task, **callbacks)


def format_event(event: RunEvent) -> Text:
    """Format a CLI event for the live log in a human-readable way."""
    prefix = {
        EventKind.TEXT: "",
        EventKind.TOOL: "⚙ ",
        EventKind.INFO: "· ",
        EventKind.ERROR: "✗ ",
    }.get(event.kind, "")
    style = {
        EventKind.TEXT: "",
        EventKind.TOOL: "cyan",
        EventKind.INFO: "dim",
        EventKind.ERROR: "bold red",
    }.get(event.kind, "")
    lines = (event.text or "").splitlines() or [""]
    return Text("\n".join(f"{prefix}{line}" for line in lines), style=style)


class TaskRuntime:
    def __init__(self, task: Task, orchestrator_factory=_default_orchestrator):
        self.task = task
        self._orchestrator_factory = orchestrator_factory
        self._app = None
        self.worker: Worker | None = None
        self.running = False
        self.log: list[Text] = live_log.load(task.id, _MAX_LOG_ENTRIES)  # persisted history
        self.phase_label = ""
        self.phase_started_at: float | None = None
        self.last_activity = 0.0
        self.event_count = 0
        self.pending_plan_confirm = False
        self._plan_then_ask = False
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------ #
    # UI subscriptions
    # ------------------------------------------------------------ #
    def add_listener(self, listener: Listener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(self, kind: str, payload: object) -> None:
        for listener in list(self._listeners):
            listener(kind, payload)

    # ------------------------------------------------------------ #
    # Orchestrator callbacks
    # ------------------------------------------------------------ #
    def _cb_state(self, task: Task) -> None:
        self._emit("state", task)

    def _cb_event(self, phase: str, event: RunEvent) -> None:
        self.event_count += 1
        self.last_activity = time.monotonic()
        self._append_log(format_event(event))

    def _cb_info(self, message: str) -> None:
        for line in message.splitlines():
            self._append_log(Text(f"◆ {line}", style="bold magenta"))

    def _cb_activity(self, phase: str) -> None:
        self.last_activity = time.monotonic()

    def _append_log(self, entry: Text) -> None:
        self.log.append(entry)
        if len(self.log) > _MAX_LOG_ENTRIES:
            del self.log[: _MAX_LOG_ENTRIES // 2]
        live_log.append(self.task.id, entry)  # survives app restarts
        self._emit("log", entry)

    # ------------------------------------------------------------ #
    # Run lifecycle
    # ------------------------------------------------------------ #
    def start(self, app, runner: Runner, label: str, *, plan_then_ask: bool = False) -> bool:
        """Start the pipeline in an App worker. False if it was already running."""
        if self.running:
            return False
        self._app = app
        self.running = True
        self._plan_then_ask = plan_then_ask
        self.pending_plan_confirm = False
        self.phase_label = label
        self.phase_started_at = time.monotonic()
        self.last_activity = time.monotonic()
        self.event_count = 0
        self._cb_info(f"▶ {label}")
        self.worker = app.run_worker(
            self._wrap(runner),
            exclusive=True,
            exit_on_error=False,
            group=f"grafeno-task-{self.task.id}",
        )
        self._emit("state", self.task)
        return True

    async def _wrap(self, runner: Runner) -> None:
        if self.task.is_remote:
            from .. import remote as remote_module

            try:
                await remote_module.pull_task_for(self.task, on_info=self._cb_info)
                self.task = models.load(self.task.id)
            except Exception as exc:  # noqa: BLE001 - sync never breaks the run
                self._cb_info(t("remote.sync.pull.fail", error=exc))
        orchestrator = self._orchestrator_factory(
            self.task,
            on_state=self._cb_state,
            on_event=self._cb_event,
            on_info=self._cb_info,
            on_activity=self._cb_activity,
        )
        try:
            await runner(orchestrator)
        except asyncio.CancelledError:
            self._cb_info(t("rt.cancelled"))
            self.task.state = TaskState.PAUSED
            models.save(self.task)
        except Exception as exc:  # noqa: BLE001 - last line of defense
            self._cb_info(t("rt.unexpected", error=exc))
        finally:
            self.running = False
            self.phase_started_at = None
            if self._plan_then_ask:
                self._plan_then_ask = False
                if self.task.state is TaskState.PLANNED:
                    self.pending_plan_confirm = True
            self._emit("state", self.task)
            app = self._app
            if app is not None and self.task.state is TaskState.DONE:
                notify = getattr(app, "task_finished", None)
                if notify is not None:
                    notify(self.task)

    def cancel(self) -> None:
        """Cancel the running worker (non-blocking)."""
        if self.worker is not None and self.running:
            self.worker.cancel()
