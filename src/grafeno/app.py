"""Aplicación TUI de GRAFENO."""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from . import __version__
from .models import Task
from .tui.runtime import TaskRuntime
from .tui.screens.tasks import TaskListScreen


class GrafenoApp(App):
    TITLE = "GRAFENO"
    SUB_TITLE = f"v{__version__} · orquestador multi-CLI"
    CSS_PATH = "grafeno.tcss"
    BINDINGS = [Binding("ctrl+q", "quit", "Salir", show=False)]

    def __init__(self):
        super().__init__()
        # Motores de tareas en segundo plano: sobreviven a la navegación.
        self.runtimes: dict[str, TaskRuntime] = {}

    def on_mount(self) -> None:
        self.push_screen(TaskListScreen())

    def runtime_for(self, task: Task) -> TaskRuntime:
        """Devuelve (o crea) el runtime de una tarea, refrescando su estado."""
        runtime = self.runtimes.get(task.id)
        if runtime is None:
            runtime = TaskRuntime(task)
            self.runtimes[task.id] = runtime
        elif not runtime.running:
            runtime.task = task  # objeto recién cargado de disco
        return runtime


def main() -> None:
    GrafenoApp().run()


if __name__ == "__main__":
    main()
