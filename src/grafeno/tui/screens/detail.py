"""Pantalla de detalle de tarea: fases, planes, revisiones y log en vivo."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ... import models, paths
from ...drivers.base import EventKind, RunEvent
from ...models import Task, TaskState
from ...pipeline.orchestrator import PHASE_LABELS, Orchestrator
from ..widgets import PhaseBar, markdown_set


class FileList(ListView):
    """Lista de archivos Markdown de un directorio."""

    def load_dir(self, directory: Path) -> None:
        self.clear()
        for entry in sorted(directory.glob("*.md")):
            self.append(ListItem(Label(entry.name), id=f"f-{entry.name}"))


class TaskDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("p", "run_plan", "Plan"),
        Binding("i", "run_implement", "Implementar"),
        Binding("r", "run_review", "Revisar"),
        Binding("f", "run_fix", "Corregir"),
        Binding("t", "run_tests", "Tests"),
        Binding("a", "run_automode", "Automode"),
        Binding("x", "cancel", "Cancelar"),
        Binding("escape", "back", "Volver"),
    ]

    def __init__(self, task: Task):
        super().__init__()
        self.current_task = task
        self._worker = None
        self._pipeline_running = False

    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"[b]{self.current_task.name}[/b]  ·  {self.current_task.workdir}", id="task-title")
        yield PhaseBar(self.current_task.state, self.current_task.iteration, id="phase-bar")
        with TabbedContent(id="tabs"):
            with TabPane("Plan", id="tab-plan"):
                with Horizontal():
                    yield FileList(id="plan-files")
                    yield Markdown("", id="plan-view")
            with TabPane("Revisiones", id="tab-review"):
                with Horizontal():
                    yield FileList(id="review-files")
                    yield Markdown("", id="review-view")
            with TabPane("Registro", id="tab-log"):
                yield RichLog(id="live-log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        self._reload_files()
        self._log_info(
            f"Tarea {self.current_task.id} · implementador: {self.current_task.implementer.cli}"
            f" ({self.current_task.implementer.model or 'default'})"
        )
        if self.current_task.branch:
            self._log_info(f"Rama git: {self.current_task.branch}")

    # ------------------------------------------------------------------ #
    # Utilidades de UI
    # ------------------------------------------------------------------ #
    def _log(self) -> RichLog:
        return self.query_one("#live-log", RichLog)

    def _log_event(self, phase: str, event: RunEvent) -> None:
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
        for line in (event.text or "").splitlines() or [""]:
            self._log().write(Text(f"{prefix}{line}", style=style))

    def _log_info(self, message: str) -> None:
        for line in message.splitlines():
            self._log().write(Text(f"◆ {line}", style="bold magenta"))

    def _state_changed(self, task: Task) -> None:
        self.query_one(PhaseBar).set_state(task.state, task.iteration)

    def _reload_files(self) -> None:
        self.query_one("#plan-files", FileList).load_dir(paths.plan_dir(self.current_task.id))
        self.query_one("#review-files", FileList).load_dir(paths.review_dir(self.current_task.id))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        if list_id not in {"plan-files", "review-files"}:
            return
        directory = paths.plan_dir(self.current_task.id) if list_id == "plan-files" else paths.review_dir(self.current_task.id)
        name = event.item.id[2:] if event.item.id else ""
        target = directory / name
        view_id = "#plan-view" if list_id == "plan-files" else "#review-view"
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            self.notify(f"No se pudo leer {name}: {exc}", severity="error")
            return
        await markdown_set(self.query_one(view_id, Markdown), text)

    # ------------------------------------------------------------------ #
    # Ejecución del pipeline
    # ------------------------------------------------------------------ #
    def _orchestrator(self) -> Orchestrator:
        return Orchestrator(
            self.current_task,
            on_state=self._state_changed,
            on_event=self._log_event,
            on_info=self._log_info,
        )

    def _start(self, runner: Callable[[Orchestrator], Awaitable[None]], label: str) -> None:
        if self._pipeline_running:
            self.notify("Ya hay una fase en ejecución (pulsa [x] para cancelar).", severity="warning")
            return
        self._pipeline_running = True
        self._log_info(f"▶ {label}")
        self._worker = self.run_worker(
            self._wrap(runner), exclusive=True, exit_on_error=False, group="pipeline"
        )

    async def _wrap(self, runner: Callable[[Orchestrator], Awaitable[None]]) -> None:
        orchestrator = self._orchestrator()
        try:
            await runner(orchestrator)
        except asyncio.CancelledError:
            self._log_info("Ejecución cancelada por el usuario.")
            self.current_task.state = TaskState.PAUSED
            models.save(self.current_task)
            self._state_changed(self.current_task)
        except Exception as exc:  # noqa: BLE001 — última línea de defensa de la TUI
            self._log_info(f"Error inesperado: {exc}")
        finally:
            self._pipeline_running = False
            self._reload_files()
            self._state_changed(self.current_task)

    # ------------------------------------------------------------------ #
    # Acciones
    # ------------------------------------------------------------------ #
    def action_run_plan(self) -> None:
        self._start(lambda orch: orch.run_plan(), PHASE_LABELS["plan"])

    def action_run_implement(self) -> None:
        if not list(paths.plan_dir(self.current_task.id).glob("*.md")):
            self.notify("Primero genera el plan ([p]).", severity="warning")
            return
        self._start(lambda orch: orch.run_implement(), PHASE_LABELS["implement"])

    def action_run_review(self) -> None:
        if self.current_task.state not in {TaskState.IMPLEMENTED, TaskState.PAUSED, TaskState.FAILED}:
            self.notify("La revisión requiere una implementación previa ([i]).", severity="warning")
            return
        self._start(lambda orch: orch.run_review(), PHASE_LABELS["review"])

    def action_run_fix(self) -> None:
        if self.current_task.iteration == 0 and not list(paths.review_dir(self.current_task.id).glob("*.md")):
            self.notify("Aún no hay revisión que corregir ([r]).", severity="warning")
            return
        self._start(lambda orch: orch.run_fix(), PHASE_LABELS["fix"])

    def action_run_tests(self) -> None:
        if not self.current_task.test_command.strip():
            self.notify("Esta tarea no define comando de tests.", severity="warning")
            return

        async def _tests(orch: Orchestrator) -> None:
            ok = await orch.run_tests()
            self._log_info("Tests en verde." if ok else "Los tests han fallado.")

        self._start(_tests, PHASE_LABELS["tests"])

    def action_run_automode(self) -> None:
        self._start(lambda orch: orch.run_automode(), "Automode")

    async def action_cancel(self) -> None:
        if self._pipeline_running and self._worker is not None:
            await self._worker.cancel()

    def action_back(self) -> None:
        if self._pipeline_running:
            self.notify("Cancela la ejecución ([x]) antes de volver.", severity="warning")
            return
        self.dismiss()
