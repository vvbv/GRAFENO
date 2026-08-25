"""Aplicación TUI de GRAFENO."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from textual.app import App
from textual.binding import Binding

from . import __version__, models, paths, scheduler
from .i18n import t
from .models import Task, TaskState
from .tui.runtime import TaskRuntime


class GrafenoApp(App):
    TITLE = "GRAFENO"
    CSS_PATH = "grafeno.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", t("common.quit"), show=False),
        Binding("super+q", "quit", t("common.quit"), show=False),  # Cmd+Q en macOS
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
        # Tick del planificador: arranca tareas programadas, encadenadas y
        # repeticiones desatendidas cuando les toca.
        self.set_interval(10.0, self._scheduler_tick)

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

    # ------------------------------------------------------------------ #
    # Planificador (tareas programadas, encadenadas y repetitivas)
    # ------------------------------------------------------------------ #
    def _scheduler_tick(self) -> None:
        """Revisa tareas programadas/repetitivas y arranca las que tocan."""
        tasks = models.list_all()
        by_id = {task.id: task for task in tasks}
        now = datetime.now()
        for task in tasks:
            runtime = self.runtimes.get(task.id)
            if runtime is not None and runtime.running:
                continue
            if not (scheduler.is_due(task, now) and scheduler.parent_done(task, by_id)):
                continue
            self._start_unattended(task, t("sched.trigger"))

    def task_finished(self, task: Task) -> None:
        """Gancho al completar una tarea: hijas encadenadas y repeticiones."""
        tasks = models.list_all()
        by_id = {task.id: task for task in tasks}
        try:
            finished = models.load(task.id)
        except Exception:
            return
        if self._maybe_restart(finished, by_id):
            return  # la repetición ya relanzó la tarea: no procesamos el resto
        if finished.repeat_mode:
            # Marca de referencia para el siguiente intervalo (también en
            # repeticiones no infinitas).
            finished.last_completed_at = datetime.now().isoformat(timespec="seconds")
            models.save(finished)
        self._launch_children(finished, tasks)

    def _maybe_restart(self, finished: Task, by_id: dict[str, Task]) -> bool:
        """Si la cadena entera terminó y la tarea es repetitiva infinita, reinicia.

        Devuelve ``True`` si se relanzó una repetición (en cuyo caso el llamador
        debe abandonar el procesamiento de la tarea antigua).
        """
        if finished.repeat_mode == "infinite" and scheduler.chain_completed(finished, by_id):
            self._restart_repetition(finished)
            return True
        return False

    def _launch_children(self, finished: Task, tasks: list[Task]) -> None:
        """Lanza las hijas DRAFT cuando el padre termina."""
        for child in scheduler.children(tasks, finished.id):
            if child.state is not TaskState.DRAFT:
                continue
            runtime = self.runtimes.get(child.id)
            if runtime is not None and runtime.running:
                continue
            if child.scheduled_at:
                try:
                    target = datetime.fromisoformat(child.scheduled_at)
                except ValueError:
                    target = None
                if target is not None and target > datetime.now():
                    # Tiene hora propia: ya la arrancará el tick cuando toque.
                    continue
            self._start_unattended(child, t("sched.chained", name=finished.name))

    def _restart_repetition(self, task: Task) -> None:
        """Prepara la siguiente iteración de una tarea repetitiva y la arranca."""
        task.repeat_count += 1
        scheduler.prepare_next_iteration(task)
        if task.plan_reuse == "replan":
            for plan_file in paths.plan_dir(task.id, 1).glob("*.md"):
                plan_file.unlink()
        models.save(task)
        runtime = self.runtime_for(task)
        runtime._cb_info(
            t("sched.repetition", name=task.name, n=task.repeat_count + 1)
        )
        self._start_unattended(task, t("sched.trigger"))

    def _start_unattended(self, task: Task, label: str) -> None:
        """Arranca el pipeline completo de una tarea sin interacción."""
        from .pipeline.orchestrator import repetition_runner

        runtime = self.runtime_for(task)
        if task.confirm_plan:
            runtime._cb_info(t("sched.confirm_ignored"))
        runtime._cb_info(t("sched.starting", name=task.name))
        runtime.start(self, repetition_runner(task), label)


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
