"""Orquestador del pipeline: plan -> implementación -> revisión ⇄ corrección.

Es independiente de la TUI: recibe callbacks y puede usarse en modo headless,
lo que lo hace testeable y reutilizable. Los drivers se inyectan para poder
sustituirlos por dobles en los tests.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

from .. import models, paths
from ..drivers import RunEvent, RunRequest, get_driver
from ..drivers.base import CLIDriver, EventKind, RunResult
from ..models import Task, TaskState
from ..timefmt import format_duration
from . import gitops, prompts
from .verdict import Verdict, parse_verdict

PHASE_LABELS = {
    "plan": "Plan",
    "implement": "Implementación",
    "review": "Revisión",
    "fix": "Corrección",
    "tests": "Tests",
    "grafeno": "Grafeno",
}


class PhaseError(Exception):
    """Una fase del pipeline ha fallado (el estado ya quedó en FAILED)."""


class Orchestrator:
    def __init__(
        self,
        task: Task,
        *,
        drivers: dict[str, CLIDriver] | None = None,
        on_state: Callable[[Task], None] | None = None,
        on_event: Callable[[str, RunEvent], None] | None = None,
        on_info: Callable[[str], None] | None = None,
        on_activity: Callable[[str], None] | None = None,
    ) -> None:
        self.task = task
        self._drivers = drivers
        self._on_state = on_state or (lambda task: None)
        self._on_event = on_event or (lambda phase, event: None)
        self._on_info = on_info or (lambda message: None)
        self._on_activity = on_activity or (lambda phase: None)

    # ------------------------------------------------------------------ #
    # Utilidades internas
    # ------------------------------------------------------------------ #
    def _driver(self, cli_name: str) -> CLIDriver:
        if self._drivers is not None:
            if cli_name not in self._drivers:
                raise PhaseError(f"CLI desconocido: '{cli_name}'.")
            return self._drivers[cli_name]
        try:
            return get_driver(cli_name)
        except (KeyError, NotImplementedError) as exc:
            raise PhaseError(str(exc)) from exc

    def _set_state(self, state: TaskState) -> None:
        self.task.state = state
        models.save(self.task)
        self._on_state(self.task)

    def _info(self, message: str) -> None:
        self._on_info(message)

    def _plan_files(self) -> list[Path]:
        plan_dir = paths.plan_dir(self.task.id, self.task.cycle)
        return sorted(plan_dir.glob("*.md"))

    async def _execute(
        self,
        role_name: str,
        phase: str,
        prompt: str,
        log_name: str,
        running_state: TaskState,
        done_state: TaskState,
    ) -> RunResult:
        task = self.task
        role = task.role(role_name)
        try:
            driver = self._driver(role.cli)
        except PhaseError:
            self._set_state(TaskState.FAILED)
            raise
        if not driver.is_available():
            self._set_state(TaskState.FAILED)
            raise PhaseError(
                f"El CLI '{role.cli}' ({driver.display_name}) no está instalado o no está en el PATH."
            )

        self._set_state(running_state)
        self._info(f"[{PHASE_LABELS[phase]}] {driver.display_name} · modelo: {role.model or 'default'}")
        request = RunRequest(
            prompt=prompt,
            model=role.model,
            workdir=Path(task.workdir),
            session_id=task.sessions.get(role_name) or None,
            log_path=paths.logs_dir(task.id) / log_name,
            title=f"grafeno:{task.id}",
        )
        started_at = time.monotonic()
        result = await driver.run(
            request,
            on_event=lambda event: self._on_event(phase, event),
            on_activity=lambda: self._on_activity(phase),
        )
        self._record_duration(phase, time.monotonic() - started_at)
        if result.session_id:
            task.sessions[role_name] = result.session_id
        if not result.ok:
            self._set_state(TaskState.FAILED)
            raise PhaseError(result.error or f"La fase {PHASE_LABELS[phase]} ha fallado.")
        self._set_state(done_state)
        self._info(
            f"✓ {PHASE_LABELS[phase]} completado en "
            f"{format_duration(time.monotonic() - started_at)}."
        )
        return result

    def _record_duration(self, phase: str, elapsed: float) -> None:
        durations = self.task.durations
        durations[phase] = int(durations.get(phase, 0) + round(elapsed))
        models.save(self.task)

    # ------------------------------------------------------------------ #
    # Fases
    # ------------------------------------------------------------------ #
    async def run_plan(self) -> None:
        result = await self._execute(
            "planner",
            "plan",
            prompts.plan_prompt(self.task),
            "plan.jsonl",
            TaskState.PLANNING,
            TaskState.PLANNED,
        )
        if not self._plan_files():
            # Respaldo: el planificador no escribió archivos; materializamos su salida.
            if not result.text.strip():
                self._set_state(TaskState.FAILED)
                raise PhaseError("El planificador no generó archivos de plan ni salida de texto.")
            plan_path = paths.plan_dir(self.task.id, self.task.cycle) / "01-plan.md"
            plan_path.write_text(
                f"{prompts.executor_header(self.task)}\n{prompts.executor_notice(self.task)}\n\n"
                f"# Plan: {self.task.name}\n\n{result.text.strip()}\n",
                encoding="utf-8",
            )
            self._info("El planificador no escribió archivos; se guardó su salida como 01-plan.md.")

    async def run_implement(self) -> None:
        self._ensure_branch()
        await self._execute(
            "implementer",
            "implement",
            prompts.implement_prompt(self.task),
            "implement.jsonl",
            TaskState.IMPLEMENTING,
            TaskState.IMPLEMENTED,
        )

    async def run_review(self) -> Verdict:
        review_number = self.task.iteration + 1
        result = await self._execute(
            "reviewer",
            "review",
            prompts.review_prompt(self.task, review_number),
            f"review-{review_number:02d}.jsonl",
            TaskState.REVIEWING,
            TaskState.IMPLEMENTED,
        )
        review_path = paths.review_dir(self.task.id, self.task.cycle) / f"{review_number:02d}-review.md"
        if not review_path.exists() and result.text.strip():
            # Respaldo: el revisor no escribió el archivo; guardamos su salida.
            review_path.write_text(result.text.strip() + "\n", encoding="utf-8")

        verdict = parse_verdict(result.text)
        if verdict is None:
            self._info("No se encontró veredicto; se asume CHANGES_REQUESTED.")
            verdict = Verdict.CHANGES_REQUESTED
        elif verdict is Verdict.APPROVED:
            tests_ok = await self.run_tests()
            if tests_ok:
                self._set_state(TaskState.DONE)
                self._info("Revisión aprobada y tests en verde. Tarea completada.")
            else:
                verdict = Verdict.CHANGES_REQUESTED
                self._info("Revisión aprobada pero los tests fallan; se pedirán correcciones.")
        return verdict

    async def run_fix(self) -> None:
        self.task.iteration += 1
        await self._execute(
            "implementer",
            "fix",
            prompts.fix_prompt(self.task, self.task.iteration),
            f"fix-{self.task.iteration:02d}.jsonl",
            TaskState.FIXING,
            TaskState.IMPLEMENTED,
        )

    async def run_tests(self) -> bool:
        command = self.task.test_command.strip()
        if not command:
            return True
        self._info(f"[Tests] Ejecutando: {command}")
        started_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.task.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            self._info(f"[Tests] No se pudo ejecutar: {exc}")
            return False
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            self._on_activity("tests")
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._on_event("tests", RunEvent(EventKind.INFO, line))
        returncode = await process.wait()
        self._record_duration("tests", time.monotonic() - started_at)
        self._info(
            f"[Tests] Código de salida: {returncode} "
            f"({format_duration(time.monotonic() - started_at)})"
        )
        return returncode == 0

    # ------------------------------------------------------------------ #
    # Automode
    # ------------------------------------------------------------------ #
    async def run_automode(self) -> None:
        """Pipeline completo sin pausas: plan → implementación → revisión ⇄ fix."""
        await self.run_automode_plan()
        if self.task.state is TaskState.FAILED:
            return
        await self.run_automode_continue()

    async def run_automode_plan(self) -> None:
        """Solo la fase de plan (punto de confirmación cuando confirm_plan)."""
        self.task.automode = True
        models.save(self.task)
        try:
            if not self._plan_files():
                await self.run_plan()
            else:
                self._info("Plan ya existente; se reutiliza.")
                if self.task.state is TaskState.DRAFT:
                    self._set_state(TaskState.PLANNED)
        except PhaseError as exc:
            self._info(str(exc))

    async def run_automode_continue(self) -> None:
        """Implementación + ciclo de revisión (requiere plan existente)."""
        self.task.automode = True
        models.save(self.task)
        if not self._plan_files():
            self._info("No hay archivos de plan; genera el plan primero ([p]).")
            return
        try:
            await self.run_implement()
            await self.run_tests()

            while self.task.state is not TaskState.DONE:
                await self.run_review()
                if self.task.state is TaskState.DONE:
                    break
                if self.task.iteration >= self.task.max_iterations:
                    self._set_state(TaskState.FAILED)
                    self._info(
                        f"Se alcanzó el máximo de iteraciones ({self.task.max_iterations}). "
                        "Revisa los archivos de revisión."
                    )
                    return
                await self.run_fix()
        except PhaseError as exc:
            self._info(str(exc))

    # ------------------------------------------------------------------ #
    def _ensure_branch(self) -> None:
        task = self.task
        if not task.create_branch or task.branch:
            return
        workdir = Path(task.workdir)
        if not gitops.is_git_repo(workdir):
            self._info("El directorio no es un repositorio git; se trabaja sin rama dedicada.")
            return
        branch = f"grafeno/{models.slugify(task.name)}"
        ok, message = gitops.create_branch(workdir, branch)
        self._info(message)
        if ok:
            task.branch = branch
            models.save(task)
