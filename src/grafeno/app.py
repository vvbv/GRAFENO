"""Aplicación TUI de GRAFENO."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from . import __version__
from .i18n import t
from .models import Task
from .tui.runtime import TaskRuntime


class GrafenoApp(App):
    TITLE = "GRAFENO"
    CSS_PATH = "grafeno.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", t("common.quit"), show=False),
        Binding("ctrl+t", "change_theme", t("app.bind.theme")),
    ]

    def __init__(self):
        super().__init__()
        # Motores de tareas en segundo plano: sobreviven a la navegación.
        self.runtimes: dict[str, TaskRuntime] = {}

    def on_mount(self) -> None:
        self.sub_title = t("app.subtitle", version=__version__)
        from . import config as config_module

        cfg = config_module.load()
        if cfg.theme and cfg.theme in self.available_themes:
            self.theme = cfg.theme
        from .tui.screens.tasks import TaskListScreen

        self.push_screen(TaskListScreen())

    def watch_theme(self, theme_name: str) -> None:
        """Guarda en la config la paleta elegida (p.ej. vía Ctrl+T)."""
        if not theme_name:
            return
        from . import config as config_module

        cfg = config_module.load()
        if cfg.theme != theme_name:
            cfg.theme = theme_name
            config_module.save(cfg)

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
    import argparse

    from . import config as config_module
    from . import editor
    from .i18n import set_language

    parser = argparse.ArgumentParser(prog="grafeno")
    parser.add_argument(
        "--noeditor",
        action="store_true",
        help="Do not open the configured editor on startup.",
    )
    args = parser.parse_args()

    cfg = config_module.load()
    set_language(cfg.language)
    if not args.noeditor:
        workdir = os.getcwd()
        editor_cfg = config_module.resolve_editor_config(cfg, Path(workdir))
        try:
            editor.maybe_open_editor(editor_cfg, workdir)
        except Exception:  # mejor esfuerzo: la TUI arranca aunque el editor falle
            pass
    GrafenoApp().run()


if __name__ == "__main__":
    main()
