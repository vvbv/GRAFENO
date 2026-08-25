"""Pantalla de lista de tareas + formulario de nueva tarea."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from ... import config as config_module, scheduler
from ... import models
from ...i18n import t
from ...models import Task, state_label
from ...pipeline.hooks import HOOK_STAGES, format_stages
from ...tokenfmt import format_tokens
from ..dirpicker import DirectoryPicker


class NewTaskScreen(ModalScreen[Task | None]):
    """Formulario modal para crear una tarea."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label(t("nt.title"), id="new-task-title")
            yield Label(t("nt.name"))
            yield Input(placeholder=t("nt.name.placeholder"), id="nt-name")
            yield Label(t("nt.description"))
            yield TextArea(id="nt-description")
            yield Label(t("nt.workdir"))
            yield DirectoryPicker(os.getcwd(), input_id="nt-workdir")
            yield Label(t("nt.schedule"))
            yield Input(placeholder=t("nt.schedule.placeholder"), id="nt-schedule")
            yield Label(t("nt.parent"))
            yield Select([], id="nt-parent", allow_blank=True)
            yield Label(t("nt.repeat"))
            yield Select(
                [
                    (t("nt.repeat.none"), ""),
                    (t("nt.repeat.interval"), "interval"),
                    (t("nt.repeat.infinite"), "infinite"),
                ],
                id="nt-repeat",
                value="",
                allow_blank=False,
            )
            yield Label(t("nt.repeat.interval_minutes"))
            yield Input(placeholder="60", id="nt-repeat-minutes")
            yield Label(t("nt.plan_reuse"))
            yield Select(
                [
                    (t("nt.plan_reuse.reuse"), "reuse"),
                    (t("nt.plan_reuse.replan"), "replan"),
                    (t("nt.plan_reuse.reevaluate"), "reevaluate"),
                ],
                id="nt-plan-reuse",
                value="reuse",
                allow_blank=False,
            )
            yield Label(t("nt.tests"))
            yield Input(placeholder=t("nt.tests.placeholder"), id="nt-tests")
            with Horizontal(classes="final-prompt-row"):
                yield Label(t("nt.final_prompt"))
                yield TextArea(id="nt-final-prompt")
            yield Checkbox(t("nt.automode"), id="nt-automode")
            yield Checkbox(t("nt.confirm_plan"), id="nt-confirm-plan")
            yield Checkbox(t("nt.branch"), id="nt-branch")
            yield Label(t("nt.hook"))
            yield Input(placeholder=t("nt.hook.placeholder"), id="nt-hook-command")
            with Horizontal(classes="automode-row"):
                for stage in HOOK_STAGES:
                    yield Checkbox(t(f"hook.stage.{stage}"), id=f"nt-hook-stage-{stage}")
            yield Checkbox(t("nt.hook.both"), id="nt-hook-both")
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.create"), variant="primary", id="nt-create")
                yield Button(t("common.cancel"), id="nt-cancel")

    def on_mount(self) -> None:
        cfg = config_module.load()
        self.query_one("#nt-tests", Input).value = cfg.automode.test_command
        self.query_one("#nt-final-prompt", TextArea).text = cfg.final_prompt
        self.query_one("#nt-automode", Checkbox).value = cfg.automode.enabled
        self.query_one("#nt-confirm-plan", Checkbox).value = cfg.automode.confirm_plan
        self.query_one("#nt-branch", Checkbox).value = cfg.automode.create_branch
        # Rellena el selector de tarea padre con las tareas existentes.
        parent_select = self.query_one("#nt-parent", Select)
        parent_options = [
            (f"{task.name} ({task.id})", task.id) for task in models.list_all()
        ]
        parent_select.set_options(parent_options)
        self.query_one("#nt-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nt-cancel":
            self.dismiss(None)
            return
        self._create()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "nt-name":
            self._create()

    def _create(self) -> None:
        name = self.query_one("#nt-name", Input).value.strip()
        if not name:
            self.notify(t("nt.error.name_required"), severity="error")
            return
        workdir = Path(self.query_one("#nt-workdir", Input).value.strip() or ".").expanduser()
        if not workdir.is_dir():
            self.notify(t("nt.error.bad_dir", path=workdir), severity="error")
            return

        try:
            scheduled_at = scheduler.parse_schedule(
                self.query_one("#nt-schedule", Input).value
            )
        except ValueError:
            self.notify(t("nt.error.bad_schedule"), severity="error")
            return
        parent_value = self.query_one("#nt-parent", Select).value
        parent_id = "" if parent_value is Select.BLANK else str(parent_value)
        repeat_mode = str(self.query_one("#nt-repeat", Select).value)
        repeat_minutes_raw = self.query_one("#nt-repeat-minutes", Input).value.strip()
        repeat_minutes = 60
        if repeat_mode == "interval":
            if not repeat_minutes_raw.isdigit() or int(repeat_minutes_raw) < 1:
                self.notify(t("nt.error.bad_interval"), severity="error")
                return
            repeat_minutes = int(repeat_minutes_raw)
        plan_reuse = str(self.query_one("#nt-plan-reuse", Select).value)
        cfg = config_module.load()
        automode_value = self.query_one("#nt-automode", Checkbox).value
        # Las tareas repetitivas se ejecutan en automode: si el usuario lo
        # dejó desactivado, lo activamos silenciosamente y avisamos.
        if repeat_mode and not automode_value:
            automode_value = True
            self.notify(t("nt.repeat.forces_automode"), severity="information")
        task = models.Task.create(
            name=name,
            description=self.query_one("#nt-description", TextArea).text.strip(),
            workdir=str(workdir.resolve()),
            config=cfg,
            automode=automode_value,
            test_command=self.query_one("#nt-tests", Input).value.strip(),
            create_branch=self.query_one("#nt-branch", Checkbox).value,
            confirm_plan=self.query_one("#nt-confirm-plan", Checkbox).value,
            final_prompt=self.query_one("#nt-final-prompt", TextArea).text.strip(),
            hook_command=self.query_one("#nt-hook-command", Input).value.strip(),
            hook_stages=format_stages([
                stage for stage in HOOK_STAGES
                if self.query_one(f"#nt-hook-stage-{stage}", Checkbox).value
            ]),
            hook_mode="both" if self.query_one("#nt-hook-both", Checkbox).value else "override",
            scheduled_at=scheduled_at,
            parent_id=parent_id,
            repeat_mode=repeat_mode,
            repeat_interval_minutes=repeat_minutes,
            plan_reuse=plan_reuse,
        )
        models.save(task)
        self.dismiss(task)


_QUIT_KEY_LABEL = "Cmd+Q" if sys.platform == "darwin" else "Ctrl+Q"


class TaskListScreen(Screen[None]):
    """Listado principal de tareas."""

    BINDINGS = [
        Binding("n", "new_task", t("tasks.bind.new")),
        Binding("c", "config", t("tasks.bind.config")),
        Binding("enter", "open_task", t("tasks.bind.open")),
        Binding("r", "reload", t("tasks.bind.reload")),
        Binding("v", "toggle_scope", t("tasks.bind.scope")),
        Binding("q", "quit_hint", t("common.quit")),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Por defecto: solo tareas del proyecto actual.
        self._show_all = False
        self._all_tasks: list[Task] = []
        self._tasks: list[Task] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="tasks-header"):
            yield Static(t("tasks.subtitle"), id="subtitle")
            yield Static("", id="clock")
            yield Button(t("tasks.scope.project"), id="scope-toggle")
        yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty-hint")
        yield Static("", id="token-summary")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(
            t("tasks.col.task"),
            t("tasks.col.state"),
            t("tasks.col.iter"),
            t("tasks.col.tokens"),
            t("tasks.col.updated"),
            t("tasks.col.workdir"),
        )
        self._reload()
        table.focus()
        # Refresco periódico: muestra el progreso de tareas en segundo plano.
        self.set_interval(2.0, self._tick_refresh)
        # Reloj en vivo: un segundo basta.
        self._render_clock()
        self.set_interval(1.0, self._render_clock)

    def on_screen_resume(self) -> None:
        self._reload()

    def _tick_refresh(self) -> None:
        runtimes = getattr(self.app, "runtimes", {})
        if any(runtime.running for runtime in runtimes.values()):
            self._reload(preserve_cursor=True)

    def _render_clock(self) -> None:
        """Reloj de 1s: hora actual siempre visible en el listado."""
        self.query_one("#clock", Static).update(
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )

    def action_toggle_scope(self) -> None:
        """Alterna entre tareas del proyecto y todas las tareas."""
        self._show_all = not self._show_all
        self.query_one("#scope-toggle", Button).label = t(
            "tasks.scope.all" if self._show_all else "tasks.scope.project"
        )
        self._reload()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "scope-toggle":
            self.action_toggle_scope()

    def _reload(self, *, preserve_cursor: bool = False) -> None:
        table = self.query_one(DataTable)
        selected = self._selected_task_id() if preserve_cursor else None
        table.clear()
        self._all_tasks = models.list_all()
        if self._show_all:
            self._tasks = list(self._all_tasks)
        else:
            cwd = str(Path.cwd().resolve())
            self._tasks = [
                task for task in self._all_tasks
                if str(Path(task.workdir).resolve()) == cwd
                or (task.parent_id and any(
                    parent.id == task.parent_id
                    and str(Path(parent.workdir).resolve()) == cwd
                    for parent in self._all_tasks
                ))
            ]
        runtimes = getattr(self.app, "runtimes", {})
        ordered = scheduler.tree_order(self._tasks)
        for index, (task, depth) in enumerate(ordered):
            runtime = runtimes.get(task.id)
            running = runtime is not None and runtime.running
            indent = "  " * depth + ("+ " if depth else "")
            name = f"{indent}{task.name}"
            if running:
                name = f"▶ {name}"  # mantiene el marcador de ejecución
            table.add_row(
                name,
                state_label(task.state),
                str(task.iteration),
                self._format_task_tokens(task),
                task.updated_at.replace("T", " "),
                task.workdir,
                key=task.id,
            )
            if selected == task.id:
                table.move_cursor(row=index)
        hint = self.query_one("#empty-hint", Static)
        hint.update("" if self._tasks else t("tasks.empty_hint"))
        self._render_token_summary()

    @staticmethod
    def _format_task_tokens(task: Task) -> str:
        """Celda 'in/out' compacta; vacía si la tarea no tiene uso aún."""
        total_in, total_out = task.token_totals()
        if total_in == 0 and total_out == 0:
            return ""
        return f"↑{format_tokens(total_in)} ↓{format_tokens(total_out)}"

    def _render_token_summary(self) -> None:
        """Resumen global de tokens por CLI+modelo (todas las tareas)."""
        summary = self.query_one("#token-summary", Static)
        totals: dict[str, list[int]] = {}
        for task in self._tasks:
            for label, (label_in, label_out) in task.tokens_by_cli_model().items():
                entry = totals.setdefault(label, [0, 0])
                entry[0] += label_in
                entry[1] += label_out
        if not totals:
            summary.update(t("tasks.tokens.empty"))
            return
        parts = [
            f"{label}: ↑{format_tokens(pair[0])} ↓{format_tokens(pair[1])}"
            for label, pair in sorted(
                totals.items(),
                key=lambda item: (-(item[1][0] + item[1][1]), item[0]),
            )
        ]
        summary.update(t("tasks.tokens.summary", summary=" · ".join(parts)))

    def _selected_task_id(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return str(row_key.value) if row_key.value is not None else None

    def action_new_task(self) -> None:
        def opened(task: Task | None) -> None:
            if task is not None:
                self._open(task.id)

        self.app.push_screen(NewTaskScreen(), opened)

    def action_config(self) -> None:
        from .config import ConfigScreen

        self.app.push_screen(ConfigScreen())

    def action_open_task(self) -> None:
        task_id = self._selected_task_id()
        if task_id:
            self._open(task_id)

    def action_reload(self) -> None:
        self._reload()

    def action_quit_hint(self) -> None:
        """Bloquea el cierre con q: salir solo es posible con el atajo de salida."""
        self.notify(t("tasks.quit_hint", key=_QUIT_KEY_LABEL), severity="warning")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self._open(str(event.row_key.value))

    def _open(self, task_id: str) -> None:
        from .detail import TaskDetailScreen

        try:
            task = models.load(task_id)
        except Exception as exc:
            self.notify(t("tasks.error.load", error=exc), severity="error")
            return
        self.app.push_screen(TaskDetailScreen(task))
