"""Pantalla de lista de tareas + formulario de nueva tarea."""

from __future__ import annotations

import os
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
    Static,
    TextArea,
)

from ... import config as config_module
from ... import models
from ...models import STATE_LABELS, Task


class NewTaskScreen(ModalScreen[Task | None]):
    """Formulario modal para crear una tarea."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label("Nueva tarea", id="new-task-title")
            yield Label("Nombre")
            yield Input(placeholder="p. ej. Añadir endpoint /health", id="nt-name")
            yield Label("Descripción")
            yield TextArea(id="nt-description")
            yield Label("Directorio del proyecto")
            yield Input(value=os.getcwd(), id="nt-workdir")
            yield Label("Comando de tests (opcional)")
            yield Input(placeholder="p. ej. pytest -q", id="nt-tests")
            yield Checkbox("Automode (plan → ejecución → revisión ⇄ corrección)", id="nt-automode")
            yield Checkbox("Automode: preguntar si el plan está bien antes de implementar", id="nt-confirm-plan")
            with Horizontal(id="nt-buttons"):
                yield Button("Crear", variant="primary", id="nt-create")
                yield Button("Cancelar", id="nt-cancel")

    def on_mount(self) -> None:
        cfg = config_module.load()
        self.query_one("#nt-tests", Input).value = cfg.automode.test_command
        self.query_one("#nt-automode", Checkbox).value = cfg.automode.enabled
        self.query_one("#nt-confirm-plan", Checkbox).value = cfg.automode.confirm_plan
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
            self.notify("El nombre es obligatorio.", severity="error")
            return
        workdir = Path(self.query_one("#nt-workdir", Input).value.strip() or ".").expanduser()
        if not workdir.is_dir():
            self.notify(f"El directorio no existe: {workdir}", severity="error")
            return
        cfg = config_module.load()
        task = models.Task.create(
            name=name,
            description=self.query_one("#nt-description", TextArea).text.strip(),
            workdir=str(workdir.resolve()),
            config=cfg,
            automode=self.query_one("#nt-automode", Checkbox).value,
            test_command=self.query_one("#nt-tests", Input).value.strip(),
            confirm_plan=self.query_one("#nt-confirm-plan", Checkbox).value,
        )
        models.save(task)
        self.dismiss(task)


class TaskListScreen(Screen[None]):
    """Listado principal de tareas."""

    BINDINGS = [
        Binding("n", "new_task", "Nueva"),
        Binding("c", "config", "Configuración"),
        Binding("enter", "open_task", "Abrir"),
        Binding("r", "reload", "Recargar"),
        Binding("q", "quit", "Salir"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Orquestador de tareas · plan → implementación → revisión",
            id="subtitle",
        )
        yield DataTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="empty-hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Tarea", "Estado", "Iter.", "Actualizada", "Directorio")
        self._reload()
        table.focus()
        # Refresco periódico: muestra el progreso de tareas en segundo plano.
        self.set_interval(2.0, self._tick_refresh)

    def on_screen_resume(self) -> None:
        self._reload()

    def _tick_refresh(self) -> None:
        runtimes = getattr(self.app, "runtimes", {})
        if any(runtime.running for runtime in runtimes.values()):
            self._reload(preserve_cursor=True)

    def _reload(self, *, preserve_cursor: bool = False) -> None:
        table = self.query_one(DataTable)
        selected = self._selected_task_id() if preserve_cursor else None
        table.clear()
        self._tasks = models.list_all()
        runtimes = getattr(self.app, "runtimes", {})
        for index, task in enumerate(self._tasks):
            runtime = runtimes.get(task.id)
            running = runtime is not None and runtime.running
            name = f"▶ {task.name}" if running else task.name
            state_label = STATE_LABELS.get(task.state, task.state.value)
            table.add_row(
                name,
                state_label,
                str(task.iteration),
                task.updated_at.replace("T", " "),
                task.workdir,
                key=task.id,
            )
            if selected == task.id:
                table.move_cursor(row=index)
        hint = self.query_one("#empty-hint", Static)
        hint.update("" if self._tasks else "No hay tareas. Pulsa [n] para crear la primera.")

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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value:
            self._open(str(event.row_key.value))

    def _open(self, task_id: str) -> None:
        from .detail import TaskDetailScreen

        try:
            task = models.load(task_id)
        except Exception as exc:
            self.notify(f"No se pudo cargar la tarea: {exc}", severity="error")
            return
        self.app.push_screen(TaskDetailScreen(task))
