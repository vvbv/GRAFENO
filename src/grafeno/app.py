"""GRAFENO TUI application."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App
from textual.binding import Binding

from . import __version__, models, paths, remotesession, scheduler
from .i18n import t
from .models import Task, TaskState
from .tui.runtime import TaskRuntime

if TYPE_CHECKING:
    from .telegram.service import TelegramService


def _window_title() -> str:
    """Terminal window title: ``Grafeno - <project dir or session target>``."""
    if remotesession.active():
        return f"Grafeno - {remotesession.label()}"
    name = Path(os.getcwd()).name
    return f"Grafeno - {name}" if name else "Grafeno"


def _resolve_remote_password(flag_value: str) -> str:
    """Password from --remote-password, GRAFENO_REMOTE_PASSWORD or a prompt."""
    if flag_value and flag_value != "-":
        return flag_value
    from_env = os.environ.get("GRAFENO_REMOTE_PASSWORD", "")
    if from_env:
        return from_env
    if flag_value == "-":
        import getpass

        return getpass.getpass(t("rsession.password_prompt"))
    return ""


class GrafenoApp(App):
    TITLE = "Grafeno"
    CSS_PATH = "grafeno.tcss"
    BINDINGS = [
        Binding("ctrl+q", "quit", t("common.quit"), show=False),
        Binding("super+q", "quit", t("common.quit"), show=False),  # Cmd+Q on macOS
        Binding("ctrl+t", "change_theme", t("app.bind.theme")),
    ]

    def __init__(self):
        super().__init__()
        self.title = _window_title()
        # Background task runtimes: they survive navigation.
        self.runtimes: dict[str, TaskRuntime] = {}
        # Telegram bot service (None when disabled or misconfigured).
        self.telegram: TelegramService | None = None

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
        if remotesession.active():
            self.notify(t("rsession.active", target=remotesession.label()), timeout=8)
        # Scheduler tick: starts scheduled, chained and unattended
        # repetitions when it is their turn.
        self.set_interval(10.0, self._scheduler_tick)
        if cfg.telegram.enabled:
            self._start_telegram(cfg)

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

    def _start_telegram(self, cfg) -> None:
        """Start the Telegram bot worker when enabled and a token is available."""
        from .telegram.service import TelegramService

        if not cfg.telegram.resolve_token():
            self.notify(t("tg.no_token"), severity="warning", timeout=10)
            return
        self.telegram = TelegramService(
            cfg.telegram,
            default_workdir=cfg.telegram.default_workdir or os.getcwd(),
            parser_cli=cfg.telegram.parser_cli or cfg.planner.cli,
            parser_model=cfg.telegram.parser_model or cfg.planner.model,
            on_info=lambda message: self.notify(message, timeout=8),
        )
        self.run_worker(
            self.telegram.run(), exclusive=True,
            group="telegram", exit_on_error=False,
        )

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
        if self.telegram is not None:
            self.run_worker(
                self.telegram.notify_task_finished(finished),
                exclusive=False, group="telegram-notify", exit_on_error=False,
            )
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
    from . import config as config_module
    from . import editor
    from .i18n import set_language

    parser = argparse.ArgumentParser(prog="grafeno")
    parser.add_argument(
        "remote",
        nargs="?",
        default="",
        help="Remote session host: [user@]host[:port] or ssh://[user@]host[:port]",
    )
    parser.add_argument("--remote-key", default="", help="SSH identity file (ssh -i).")
    parser.add_argument(
        "--remote-password",
        nargs="?",
        const="-",
        default="",
        help="SSH password; without value, prompts interactively.",
    )
    parser.add_argument("--remote-port", type=int, default=0, help="SSH port.")
    parser.add_argument(
        "--noeditor",
        action="store_true",
        help="Do not open the configured editor on startup.",
    )
    args = parser.parse_args()

    # Bootstrap the remote session BEFORE loading the config: that way
    # ``GRAFENO_HOME`` already points at the remote ``~/.grafeno`` mount
    # when ``config_module.load()`` reads ``config.toml``.
    session = None
    if args.remote:
        spec = remotesession.parse_host_spec(args.remote)
        if spec is None:
            parser.error(t("rsession.bad_spec", spec=args.remote))
        if args.remote_port:
            spec.port = args.remote_port
        password = _resolve_remote_password(args.remote_password)
        # Bootstrap messages print in English on purpose: the active
        # language has not been loaded yet (it lives in the remote
        # config). Deliberate and acceptable.
        try:
            session = asyncio.run(
                remotesession.bootstrap(
                    spec,
                    identity=args.remote_key,
                    password=password,
                    on_info=lambda message: print(message, file=sys.stderr),
                )
            )
        except remotesession.SessionError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2) from exc
        remotesession.activate(session)

    cfg = config_module.load()
    set_language(cfg.language)
    # Opening a local editor over the cwd is meaningless in session mode
    # (the project lives on the remote host); skip it then.
    if not args.noeditor and session is None:
        workdir = os.getcwd()
        editor_cfg = config_module.resolve_editor_config(cfg, Path(workdir))
        try:
            editor.maybe_open_editor(editor_cfg, workdir)
        except Exception:  # best effort: the TUI starts even if the editor fails
            pass
    GrafenoApp().run()


if __name__ == "__main__":
    main()
