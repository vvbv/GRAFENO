"""Orquestador del pipeline: plan -> implementación -> revisión ⇄ corrección -> pasos finales.

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
from ..config import RoleConfig
from ..drivers import RunEvent, RunRequest, get_driver
from ..drivers.base import CLIDriver, EventKind, RunResult
from ..i18n import t
from ..models import Task, TaskState
from ..timefmt import format_duration
from . import gitops, prompts
from .verdict import Verdict, parse_verdict


def phase_label(phase: str) -> str:
    """Etiqueta localizada de una fase del pipeline."""
    return t(f"phase.{phase}")


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
                raise PhaseError(t("orch.unknown_cli", cli=cli_name))
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
                t("orch.cli_missing", cli=role.cli, name=driver.display_name)
            )

        self._set_state(running_state)
        self._info(t("orch.phase_start", phase=phase_label(phase), driver=driver.display_name, model=role.model or "default"))
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
        self._record_tokens(role, result)
        if result.session_id:
            task.sessions[role_name] = result.session_id
        if not result.ok:
            self._set_state(TaskState.FAILED)
            raise PhaseError(result.error or t("orch.phase_failed", phase=phase_label(phase)))
        self._set_state(done_state)
        self._info(
            t("orch.phase_done", phase=phase_label(phase), duration=format_duration(time.monotonic() - started_at))
        )
        return result

    def _record_duration(self, phase: str, elapsed: float) -> None:
        durations = self.task.durations
        durations[phase] = int(durations.get(phase, 0) + round(elapsed))
        models.save(self.task)

    def _record_tokens(self, role: RoleConfig, result: RunResult) -> None:
        """Acumula en la tarea los tokens de la ejecución, por modelo."""
        if result.tokens.empty:
            return
        self.task.record_tokens(role.model, result.tokens)
        models.save(self.task)

    # ------------------------------------------------------------------ #
    # Fases
    # ------------------------------------------------------------------ #
    async def ensure_agents_md(self) -> None:
        """Genera AGENTS.md en el proyecto si no existe (mejor esfuerzo).

        Usa el rol planner (su CLI y modelo). Un fallo aquí NO falla la tarea:
        solo se informa y se continúa; los problemas reales del CLI del
        planner ya los gestiona la fase de plan con su manejo habitual.
        """
        workdir = Path(self.task.workdir)
        if (workdir / "AGENTS.md").exists():
            return
        role = self.task.role("planner")
        try:
            driver = self._driver(role.cli)
        except PhaseError:
            return
        if not driver.is_available():
            return
        self._info(
            t("orch.agents_md.generating", driver=driver.display_name, model=role.model or "default")
        )
        request = RunRequest(
            prompt=driver.build_agents_md_prompt(),
            model=role.model,
            workdir=workdir,
            log_path=paths.logs_dir(self.task.id) / "agents-md.jsonl",
            title=f"grafeno:{self.task.id}:agents-md",
        )
        result = await driver.run(
            request,
            on_event=lambda event: self._on_event("plan", event),
            on_activity=lambda: self._on_activity("plan"),
        )
        self._record_tokens(role, result)
        if result.ok and (workdir / "AGENTS.md").exists():
            self._info(t("orch.agents_md.done"))
        elif result.ok:
            self._info(t("orch.agents_md.no_file"))
        else:
            self._info(t("orch.agents_md.failed", error=result.error or "?"))

    async def run_plan(self) -> None:
        await self.ensure_agents_md()
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
                raise PhaseError(t("orch.no_plan_output"))
            plan_path = paths.plan_dir(self.task.id, self.task.cycle) / "01-plan.md"
            plan_path.write_text(
                f"{prompts.executor_header(self.task)}\n{prompts.executor_notice(self.task)}\n\n"
                f"# Plan: {self.task.name}\n\n{result.text.strip()}\n",
                encoding="utf-8",
            )
            self._info(t("orch.plan_fallback"))

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
            self._info(t("orch.no_verdict"))
            verdict = Verdict.CHANGES_REQUESTED
        elif verdict is Verdict.APPROVED:
            tests_ok = await self.run_tests()
            if tests_ok:
                self._set_state(TaskState.DONE)
                self._info(t("orch.approved"))
            else:
                verdict = Verdict.CHANGES_REQUESTED
                self._info(t("orch.approved_tests_fail"))
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

    async def run_final(self) -> None:
        result = await self._execute(
            "final",
            "final",
            prompts.final_prompt(self.task),
            "final.jsonl",
            TaskState.FINALIZING,
            TaskState.DONE,
        )
        final_path = paths.final_dir(self.task.id, self.task.cycle) / "01-final.md"
        if not final_path.exists() and result.text.strip():
            # Respaldo: el agente no escribió el informe; guardamos su salida.
            final_path.write_text(result.text.strip() + "\n", encoding="utf-8")

    async def run_tests(self) -> bool:
        command = self.task.test_command.strip()
        if not command:
            return True
        self._info(t("orch.tests.run", command=command))
        started_at = time.monotonic()
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                cwd=self.task.workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            self._info(t("orch.tests.exec_error", error=exc))
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
            t("orch.tests.exit", code=returncode, duration=format_duration(time.monotonic() - started_at))
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
                self._info(t("orch.plan_reused"))
                if self.task.state is TaskState.DRAFT:
                    self._set_state(TaskState.PLANNED)
        except PhaseError as exc:
            self._info(str(exc))

    async def run_automode_continue(self) -> None:
        """Implementación + ciclo de revisión (requiere plan existente)."""
        self.task.automode = True
        models.save(self.task)
        if not self._plan_files():
            self._info(t("orch.no_plan_files"))
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
                        t("orch.max_iterations", max=self.task.max_iterations)
                    )
                    return
                await self.run_fix()
            if self.task.state is TaskState.DONE:
                await self.run_final()
        except PhaseError as exc:
            self._info(str(exc))

    # ------------------------------------------------------------------ #
    def _ensure_branch(self) -> None:
        task = self.task
        if not task.create_branch or task.branch:
            return
        workdir = Path(task.workdir)
        if not gitops.is_git_repo(workdir):
            self._info(t("orch.not_git"))
            return
        branch = f"grafeno/{models.slugify(task.name)}"
        ok, message = gitops.create_branch(workdir, branch)
        self._info(message)
        if ok:
            task.branch = branch
            models.save(task)
