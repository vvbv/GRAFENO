"""GRAFENO TUI application."""

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
        Binding("super+q", "quit", t("common.quit"), show=False),  # Cmd+Q on macOS
        Binding("ctrl+t", "change_theme", t("app.bind.theme")),
    ]

    def __init__(self):
        super().__init__()
        # Background task runtimes: they survive navigation.
        self.runtimes: dict[str, TaskRuntime] = {}

    def on_mount(self) -> None:
        self.sub_title = t("app.subtitle", version=__version__)
        from . import config as config_module

        cfg = config_module.load()
        if cfg.theme and cfg.theme in self.available_themes:
            self.theme = cfg.theme
        from .tui.screens.tasks import TaskListScreen

        self.push_screen(TaskListScreen())
        if cfg.auto_update:
            self.run_worker(
                self._auto_update(), exclusive=True,
                group="auto-update", exit_on_error=False,
            )
        if not self._clis_available():
            self.notify(t("app.no_clis"), severity="warning", timeout=10)
        # Scheduler tick: starts scheduled, chained and unattended
        # repetitions when it is their turn.
        self.set_interval(10.0, self._scheduler_tick)

    def watch_theme(self, theme_name: str) -> None:
        """Persist the chosen palette in the config (e.g. via Ctrl+T)."""
        if not theme_name:
            return
        from . import config as config_module

        cfg = config_module.load()
        if cfg.theme != theme_name:
            cfg.theme = theme_name
            config_module.save(cfg)

    def runtime_for(self, task: Task) -> TaskRuntime:
        """Return (or create) the runtime for a task, refreshing its state."""
        runtime = self.runtimes.get(task.id)
        if runtime is None:
            runtime = TaskRuntime(task)
            self.runtimes[task.id] = runtime
        elif not runtime.running:
            runtime.task = task  # object freshly loaded from disk
        return runtime

    # ------------------------------------------------------------------ #
    # Scheduler (scheduled, chained and repetitive tasks)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clis_available() -> bool:
        from .drivers import available_clis

        return bool(available_clis())

    async def _auto_update(self) -> None:
        """Update the installed agent CLIs in the background (best effort)."""
        from . import updater

        outcomes = await updater.update_all()
        for outcome in outcomes:
            if not outcome.ok:
                self.notify(
                    t("upd.failed", cli=outcome.cli, error=outcome.detail or "?"),
                    severity="warning",
                )
        if outcomes and all(outcome.ok for outcome in outcomes):
            self.notify(t("upd.done"))

    def _scheduler_tick(self) -> None:
        """Check scheduled/repetitive tasks and start those that are due."""
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
        """Hook when a task finishes: chained children and repetitions."""
        tasks = models.list_all()
        by_id = {task.id: task for task in tasks}
        try:
            finished = models.load(task.id)
        except Exception:
            return
        if self._maybe_restart(finished, by_id):
            return  # the repetition already relaunched the task: skip the rest
        if finished.repeat_mode:
            # Reference timestamp for the next interval (also for
            # non-infinite repetitions).
            finished.last_completed_at = datetime.now().isoformat(timespec="seconds")
            models.save(finished)
        self._launch_children(finished, tasks)

    def _maybe_restart(self, finished: Task, by_id: dict[str, Task]) -> bool:
        """If the whole chain finished and the task is infinite-repetitive, restart.

        Returns ``True`` if a repetition was relaunched (in which case the
        caller must abandon processing of the old task).
        """
        if finished.repeat_mode == "infinite" and scheduler.chain_completed(finished, by_id):
            self._restart_repetition(finished)
            return True
        return False

    def _launch_children(self, finished: Task, tasks: list[Task]) -> None:
        """Launch DRAFT children when their parent finishes."""
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
                    # It has its own scheduled time: the tick will start it when due.
                    continue
            self._start_unattended(child, t("sched.chained", name=finished.name))

    def _restart_repetition(self, task: Task) -> None:
        """Prepare the next iteration of a repetitive task and launch it."""
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
        """Start the full pipeline of a task without user interaction."""
        if not self._clis_available():
            runtime = self.runtime_for(task)
            runtime._cb_info(t("sched.no_clis", name=task.name))
            return
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
        except Exception:  # best effort: the TUI starts even if the editor fails
            pass
    GrafenoApp().run()


if __name__ == "__main__":
    main()
