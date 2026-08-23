"""Pantalla de detalle de tarea: fases, planes, revisiones y log en vivo."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
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
    TextArea,
)

from ... import models, paths
from ...models import Task, TaskState
from ...pipeline.orchestrator import PHASE_LABELS, Orchestrator
from ...timefmt import format_duration
from ..widgets import PhaseBar, markdown_set

_SPINNER = "⠋⠙⠹⠸⠼⠴⦦⣾"
_WARN_AFTER_S = 90    # sin salida: aviso amarillo
_STALL_AFTER_S = 300  # sin salida: aviso rojo (posible bloqueo)


class FileItem(ListItem):
    """Item de lista que referencia un archivo en disco."""

    def __init__(self, path: Path, base: Path | None = None):
        label = str(path.relative_to(base)) if base and path.is_relative_to(base) else path.name
        super().__init__(Label(label))
        self.file_path = path


class FileList(ListView):
    """Lista de archivos Markdown (recursiva: incluye ciclos de ampliación)."""

    def load_dir(self, directory: Path) -> None:
        self.clear()
        for entry in sorted(directory.glob("**/*.md")):
            self.append(FileItem(entry, directory))


class PlanConfirmScreen(ModalScreen[bool]):
    """Punto de confirmación del automode: ¿el plan es correcto e implementamos?"""

    BINDINGS = [Binding("escape", "reject", "Revisar después")]

    def __init__(self, plan_count: int):
        super().__init__()
        self._plan_count = plan_count

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-confirm-dialog"):
            yield Label("Plan listo", id="plan-confirm-title")
            yield Static(
                f"El planificador generó {self._plan_count} archivo(s) de plan.\n"
                "Revísalos en la pestaña [b]Plan[/b].\n\n"
                "¿Continuar con la implementación?"
            )
            with Horizontal(id="pc-buttons"):
                yield Button("Implementar", variant="primary", id="pc-accept")
                yield Button("Revisar después", id="pc-reject")

    def action_reject(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pc-accept")


# Descripción de cada fase para el modal de confirmación.
_PHASE_INFO = {
    "plan": {
        "title": "Planificar",
        "role": "planner",
        "what": "El planificador explorará el proyecto y escribirá uno o varios\n"
        "archivos de plan en la carpeta de la tarea (pestaña Plan).",
    },
    "implement": {
        "title": "Implementar",
        "role": "implementer",
        "what": "El implementador leerá el plan y aplicará los cambios en el\n"
        "proyecto (creará la rama git de la tarea si procede).",
    },
    "review": {
        "title": "Revisar",
        "role": "reviewer",
        "what": "El revisor comprobará que los cambios cumplen el plan y\n"
        "emitirá un veredicto (APPROVED / CHANGES_REQUESTED).",
    },
    "fix": {
        "title": "Corregir",
        "role": "implementer",
        "what": "El implementador aplicará las correcciones pedidas en la\n"
        "última revisión, sin desviarse del plan.",
    },
    "tests": {
        "title": "Ejecutar tests",
        "role": None,
        "what": "Se ejecutará localmente el comando de tests de la tarea.",
    },
    "automode": {
        "title": "Automode",
        "role": None,
        "what": "Se encadenará todo el pipeline: plan → implementación →\n"
        "revisión ⇄ corrección, hasta aprobar y pasar los tests.",
    },
}


class PhaseConfirmScreen(ModalScreen[bool]):
    """Confirmación antes de lanzar una fase: explica qué va a ocurrir."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, task: Task, phase: str):
        super().__init__()
        self._gtask = task
        self._phase = phase

    def compose(self) -> ComposeResult:
        info = _PHASE_INFO[self._phase]
        with Vertical(id="plan-confirm-dialog"):
            yield Label(f"¿{info['title']}?", id="plan-confirm-title")
            yield Static(info["what"])
            if info["role"]:
                role = self._gtask.role(info["role"])
                yield Static(
                    f"[b]Agente:[/b] {role.cli} · modelo: {role.model or 'default'}",
                    classes="pc-detail",
                )
            else:
                roles = (
                    f"[b]Planificador:[/b] {self._gtask.planner.cli} ({self._gtask.planner.model or 'default'})\n"
                    f"[b]Implementador:[/b] {self._gtask.implementer.cli} ({self._gtask.implementer.model or 'default'})\n"
                    f"[b]Revisor:[/b] {self._gtask.reviewer.cli} ({self._gtask.reviewer.model or 'default'})"
                )
                if self._phase == "automode":
                    yield Static(roles, classes="pc-detail")
            yield Static(f"[b]Proyecto:[/b] {self._gtask.workdir}", classes="pc-detail")
            if self._phase == "automode" and self._gtask.confirm_plan:
                yield Static(
                    "Se pausará tras el plan para pedirte confirmación.",
                    classes="pc-detail",
                )
            if self._phase == "tests":
                yield Static(
                    f"[b]Comando:[/b] {self._gtask.test_command}", classes="pc-detail"
                )
            with Horizontal(id="pc-buttons"):
                yield Button("Ejecutar", variant="primary", id="pc-accept")
                yield Button("Cancelar", id="pc-reject")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pc-accept")


class RequestMoreScreen(ModalScreen[str | None]):
    """Pedir una ampliación: arranca un ciclo nuevo con la misma lógica."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, task: Task):
        super().__init__()
        self._gtask = task

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label(f"Pedir más · ciclo {self._gtask.cycle + 1}", id="new-task-title")
            yield Label("¿Qué más necesitas sobre este proyecto?")
            yield TextArea(id="rm-text")
            yield Static(
                f"Misma lógica: se planifica ({self._gtask.planner.cli}), "
                + ("tú apruebas, " if self._gtask.confirm_plan else "")
                + f"se implementa ({self._gtask.implementer.cli}) y se revisa "
                f"({self._gtask.reviewer.cli}).",
                classes="pc-detail",
            )
            with Horizontal(id="nt-buttons"):
                yield Button("Comenzar ciclo", variant="primary", id="rm-accept")
                yield Button("Cancelar", id="rm-cancel")

    def on_mount(self) -> None:
        self.query_one("#rm-text", TextArea).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "rm-cancel":
            self.dismiss(None)
            return
        request = self.query_one("#rm-text", TextArea).text.strip()
        if not request:
            self.notify("Describe qué necesitas.", severity="error")
            return
        self.dismiss(request)


class TaskDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("p", "run_plan", "Planificar"),
        Binding("i", "run_implement", "Implementar"),
        Binding("r", "run_review", "Revisar"),
        Binding("f", "run_fix", "Corregir"),
        Binding("t", "run_tests", "Tests"),
        Binding("a", "run_automode", "Automode"),
        Binding("m", "ask_more", "Pedir más"),
        Binding("x", "cancel", "Cancelar"),
        Binding("escape", "back", "Volver"),
    ]

    def __init__(self, task: Task):
        super().__init__()
        self.current_task = task
        self._spinner_index = 0
        self._asking_plan = False

    @property
    def runtime(self):
        """Motor en segundo plano de esta tarea (vive en la App)."""
        return self.app.runtime_for(self.current_task)

    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="task-title")
        yield PhaseBar(self.current_task.state, self.current_task.iteration, id="phase-bar")
        yield Static("", id="activity-bar")
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
        self._render_title()
        self._reload_files()
        runtime = self.runtime
        runtime.add_listener(self._on_runtime)
        if not runtime.log:
            runtime._cb_info(
                f"Tarea {self.current_task.id} · implementador: {self.current_task.implementer.cli}"
                f" ({self.current_task.implementer.model or 'default'})"
            )
            if self.current_task.branch:
                runtime._cb_info(f"Rama git: {self.current_task.branch}")
        self._replay_log()
        self._render_activity()
        # Reloj de 1s: el tick en pantalla demuestra que la UI no está congelada.
        self.set_interval(1.0, self._tick)
        self._maybe_plan_confirm()

    def on_screen_suspend(self) -> None:
        self.runtime.remove_listener(self._on_runtime)

    def on_screen_resume(self) -> None:
        # Al volver: reconectar con el pipeline que siguió corriendo.
        self.runtime.add_listener(self._on_runtime)
        self._replay_log()
        self._render_title()
        self._reload_files()
        self._render_activity()
        self._maybe_plan_confirm()

    # ------------------------------------------------------------------ #
    # Suscripción al runtime
    # ------------------------------------------------------------------ #
    def _on_runtime(self, kind: str, payload: object) -> None:
        if kind == "log":
            self._log().write(payload)
        elif kind == "state":
            self._state_changed(payload)
            self._maybe_plan_confirm()

    def _replay_log(self) -> None:
        log = self._log()
        log.clear()
        for entry in self.runtime.log:
            log.write(entry)

    # ------------------------------------------------------------------ #
    # Barra de actividad (señal de vida + watchdog + tiempos)
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        if self.runtime.running:
            self._spinner_index += 1
            self._render_activity()

    def _total_seconds(self) -> float:
        total = float(sum(self.current_task.durations.values()))
        if self.runtime.phase_started_at is not None:
            total += time.monotonic() - self.runtime.phase_started_at
        return total

    def _render_activity(self) -> None:
        bar = self.query_one("#activity-bar", Static)
        total = format_duration(self._total_seconds())
        runtime = self.runtime
        if not runtime.running or runtime.phase_started_at is None:
            if self.current_task.durations:
                bar.update(Text(f"■ En espera · tiempo total acumulado {total}", style="dim"))
            else:
                bar.update(Text("■ En espera", style="dim"))
            return

        elapsed = time.monotonic() - runtime.phase_started_at
        silence = time.monotonic() - runtime.last_activity
        spinner = _SPINNER[self._spinner_index % len(_SPINNER)]
        line = Text()
        line.append(f"{spinner} ", style="bold green")
        line.append(f"{runtime.phase_label}", style="bold")
        line.append(f" · {format_duration(elapsed)}", style="green")
        line.append(f" · {runtime.event_count} eventos", style="dim")
        if silence >= _STALL_AFTER_S:
            line.append(
                f" · ⚠ sin salida hace {format_duration(silence)}: posible bloqueo; pulsa [x] para cancelar",
                style="bold red",
            )
        elif silence >= _WARN_AFTER_S:
            line.append(
                f" · sin salida hace {format_duration(silence)} (puede estar razonando)",
                style="yellow",
            )
        else:
            line.append(f" · última salida hace {format_duration(silence)}", style="dim")
        line.append(f" · total {total}", style="dim")
        bar.update(line)

    # ------------------------------------------------------------------ #
    # Utilidades de UI
    # ------------------------------------------------------------------ #
    def _log(self) -> RichLog:
        return self.query_one("#live-log", RichLog)

    def _state_changed(self, task: Task) -> None:
        self.query_one(PhaseBar).set_state(task.state, task.iteration)
        self._render_title()
        self._reload_files()  # los artefactos aparecen al terminar cada fase

    def _render_title(self) -> None:
        cycle = f"  [b]·[/b]  ciclo {self.current_task.cycle}" if self.current_task.cycle > 1 else ""
        self.query_one("#task-title", Static).update(
            f"[b]{self.current_task.name}[/b]{cycle}  [b]·[/b]  {self.current_task.workdir}"
        )

    def _reload_files(self) -> None:
        self.query_one("#plan-files", FileList).load_dir(paths.plan_dir(self.current_task.id))
        self.query_one("#review-files", FileList).load_dir(paths.review_dir(self.current_task.id))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        if list_id not in {"plan-files", "review-files"} or not isinstance(event.item, FileItem):
            return
        target = event.item.file_path
        view_id = "#plan-view" if list_id == "plan-files" else "#review-view"
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            self.notify(f"No se pudo leer {target.name}: {exc}", severity="error")
            return
        await markdown_set(self.query_one(view_id, Markdown), text)

    # ------------------------------------------------------------------ #
    # Ejecución del pipeline (a través del runtime de la App)
    # ------------------------------------------------------------------ #
    def _start(
        self,
        runner: Callable[[Orchestrator], Awaitable[None]],
        label: str,
        plan_then_ask: bool = False,
    ) -> None:
        if not self.runtime.start(self.app, runner, label, plan_then_ask=plan_then_ask):
            self.notify("Ya hay una fase en ejecución (pulsa [x] para cancelar).", severity="warning")

    # ------------------------------------------------------------------ #
    # Acciones (todas pasan por el modal de confirmación)
    # ------------------------------------------------------------------ #
    def _confirm(
        self,
        phase: str,
        runner: Callable[[Orchestrator], Awaitable[None]],
        label: str,
        plan_then_ask: bool = False,
    ) -> None:
        if self.runtime.running:
            self.notify("Ya hay una fase en ejecución (pulsa [x] para cancelar).", severity="warning")
            return

        def decide(accepted: bool) -> None:
            if accepted:
                self._start(runner, label, plan_then_ask)

        self.app.push_screen(PhaseConfirmScreen(self.current_task, phase), decide)

    def action_run_plan(self) -> None:
        self._confirm("plan", lambda orch: orch.run_plan(), PHASE_LABELS["plan"])

    def action_run_implement(self) -> None:
        if not list(paths.plan_dir(self.current_task.id, self.current_task.cycle).glob("*.md")):
            self.notify("Primero genera el plan ([p]).", severity="warning")
            return
        self._confirm("implement", lambda orch: orch.run_implement(), PHASE_LABELS["implement"])

    def action_run_review(self) -> None:
        if self.current_task.state not in {TaskState.IMPLEMENTED, TaskState.PAUSED, TaskState.FAILED}:
            self.notify("La revisión requiere una implementación previa ([i]).", severity="warning")
            return
        self._confirm("review", lambda orch: orch.run_review(), PHASE_LABELS["review"])

    def action_run_fix(self) -> None:
        if self.current_task.iteration == 0 and not list(paths.review_dir(self.current_task.id, self.current_task.cycle).glob("*.md")):
            self.notify("Aún no hay revisión que corregir ([r]).", severity="warning")
            return
        self._confirm("fix", lambda orch: orch.run_fix(), PHASE_LABELS["fix"])

    def action_run_tests(self) -> None:
        if not self.current_task.test_command.strip():
            self.notify("Esta tarea no define comando de tests.", severity="warning")
            return

        async def _tests(orch: Orchestrator) -> None:
            ok = await orch.run_tests()
            self.runtime._cb_info("Tests en verde." if ok else "Los tests han fallado.")

        self._confirm("tests", _tests, PHASE_LABELS["tests"])

    def _pipeline_runner(self) -> tuple[Callable[[Orchestrator], Awaitable[None]], str, bool]:
        """Runner del pipeline completo respetando confirm_plan (misma lógica
        para el primer ciclo y para las ampliaciones)."""
        label = f"Ciclo {self.current_task.cycle}"
        if self.current_task.confirm_plan:
            return (lambda orch: orch.run_automode_plan()), f"{label} · Plan", True
        return (lambda orch: orch.run_automode()), f"{label} · Automode", False

    def action_run_automode(self) -> None:
        runner, label, plan_then_ask = self._pipeline_runner()
        self._confirm("automode", runner, label, plan_then_ask)

    def action_ask_more(self) -> None:
        if self.runtime.running:
            self.notify("Ya hay una fase en ejecución (pulsa [x] para cancelar).", severity="warning")
            return

        def accepted(request: str | None) -> None:
            if not request:
                return
            self.current_task.start_new_cycle(request)
            models.save(self.current_task)
            self.runtime.task = self.current_task
            self._state_changed(self.current_task)
            self.runtime._cb_info(f"▶ Ciclo {self.current_task.cycle}: {request}")
            runner, label, plan_then_ask = self._pipeline_runner()
            self._start(runner, label, plan_then_ask)

        self.app.push_screen(RequestMoreScreen(self.current_task), accepted)

    def _maybe_plan_confirm(self) -> None:
        if (
            self.runtime.pending_plan_confirm
            and self.current_task.state is TaskState.PLANNED
            and not self._asking_plan
        ):
            self.runtime.pending_plan_confirm = False
            self._ask_plan_confirmation()

    def _ask_plan_confirmation(self) -> None:
        plan_count = len(list(paths.plan_dir(self.current_task.id, self.current_task.cycle).glob("*.md")))
        self._asking_plan = True

        def answered(accepted: bool) -> None:
            self._asking_plan = False
            if accepted:
                self._start(
                    lambda orch: orch.run_automode_continue(),
                    "Automode · Implementación",
                )
            else:
                self.runtime._cb_info(
                    "Automode en pausa: revisa el plan y pulsa [a] para continuar o [i] para implementar."
                )

        self.app.push_screen(PlanConfirmScreen(plan_count), answered)

    def action_cancel(self) -> None:
        if self.runtime.running:
            self.runtime.cancel()
            self.notify("Cancelando la ejecución…")

    def action_back(self) -> None:
        # Volver nunca interrumpe: el pipeline sigue en segundo plano.
        self.dismiss()
