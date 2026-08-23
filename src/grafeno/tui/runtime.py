"""Motores de tareas en segundo plano.

Un ``TaskRuntime`` por tarea, registrado en la App (no en la pantalla): el
worker del pipeline sobrevive a la navegación entre pantallas y varias
tareas pueden ejecutarse en paralelo. Las pantallas se suscriben como
listeners para pintar el log y el estado, y se desuscriben al salir.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from rich.text import Text
from textual.worker import Worker

from .. import models
from ..drivers.base import EventKind, RunEvent
from ..models import Task, TaskState
from ..pipeline.orchestrator import Orchestrator

Runner = Callable[[Orchestrator], Awaitable[None]]
Listener = Callable[[str, object], None]  # (kind, payload): "log" | "state"

_MAX_LOG_ENTRIES = 5000


def _default_orchestrator(task: Task, **callbacks) -> Orchestrator:
    return Orchestrator(task, **callbacks)


def format_event(event: RunEvent) -> Text:
    """Da formato legible a un evento del CLI para el registro en vivo."""
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
        self.worker: Worker | None = None
        self.running = False
        self.log: list[Text] = []
        self.phase_label = ""
        self.phase_started_at: float | None = None
        self.last_activity = 0.0
        self.event_count = 0
        self.pending_plan_confirm = False
        self._plan_then_ask = False
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------ #
    # Suscripciones de la UI
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
    # Callbacks del orquestador
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
        self._emit("log", entry)

    # ------------------------------------------------------------ #
    # Ciclo de vida de la ejecución
    # ------------------------------------------------------------ #
    def start(self, app, runner: Runner, label: str, *, plan_then_ask: bool = False) -> bool:
        """Arranca el pipeline en un worker de la App. False si ya corría."""
        if self.running:
            return False
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
            self._cb_info("Ejecución cancelada por el usuario.")
            self.task.state = TaskState.PAUSED
            models.save(self.task)
        except Exception as exc:  # noqa: BLE001 — última línea de defensa
            self._cb_info(f"Error inesperado: {exc}")
        finally:
            self.running = False
            self.phase_started_at = None
            if self._plan_then_ask:
                self._plan_then_ask = False
                if self.task.state is TaskState.PLANNED:
                    self.pending_plan_confirm = True
            self._emit("state", self.task)

    def cancel(self) -> None:
        """Cancela el worker en curso (no bloqueante)."""
        if self.worker is not None and self.running:
            self.worker.cancel()
