"""Pipeline orchestrator: plan -> implementation -> review ⇄ fix -> final steps.

It is independent of the TUI: it receives callbacks and can be used in
headless mode, which makes it testable and reusable. Drivers are injected
so they can be replaced by doubles in tests.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from pathlib import Path
from typing import Awaitable, Callable

from .. import models, paths, ratelimit, remote, remotesession, triggers
from ..config import RoleConfig
from ..drivers import RunEvent, RunRequest, get_driver
from ..drivers.base import CLIDriver, EventKind, RunResult
from ..i18n import t
from ..mdnorm import normalize_markdown
from ..models import Task, TaskState
from ..timefmt import format_duration
from . import gitops, hooks, prompts
from .verdict import Verdict, parse_verdict


def phase_label(phase: str) -> str:
    """Localized label for a pipeline phase."""
    return t(f"phase.{phase}")


class PhaseError(Exception):
    """A pipeline phase failed (the state has already been set to FAILED)."""


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
    # Internal helpers
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

    def _mark_failed(self, phase: str) -> None:
        """Mark the pipeline phase as failed (recorded for resume)."""
        self.task.failed_phase = phase
        self._set_state(TaskState.FAILED)

    def _info(self, message: str) -> None:
        self._on_info(message)

    def _set_usage_waiting(self, waiting: bool) -> None:
        """Toggle the transient "waiting for quota" flag and refresh the UI."""
        if self.task.usage_waiting == waiting:
            return
        self.task.usage_waiting = waiting
        self._on_state(self.task)  # not persisted: transient flag

    def _plan_files(self) -> list[Path]:
        plan_dir = paths.plan_dir(self.task.id, self.task.cycle)
        return sorted(plan_dir.glob("*.md"))

    def _normalize_md_files(self, directory: Path) -> None:
        """Normalize the .md files of a phase directory in place (best effort)."""
        try:
            for md_file in sorted(directory.glob("*.md")):
                try:
                    original = md_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                normalized = normalize_markdown(original)
                if normalized != original:
                    try:
                        md_file.write_text(normalized, encoding="utf-8")
                    except OSError:
                        continue
        except OSError:
            pass  # missing directory: nothing to normalize

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
            self._mark_failed(phase)
            raise
        if not driver.is_available():
            self._mark_failed(phase)
            raise PhaseError(
                t("orch.cli_missing", cli=role.cli, name=driver.display_name)
            )

        self._set_state(running_state)
        await self._run_triggers(phase, "before")
        self._info(t("orch.phase_start", phase=phase_label(phase), driver=driver.display_name, model=role.model or "default"))
        request = RunRequest(
            prompt=prompt,
            model=role.model,
            workdir=remote.effective_workdir(task.remote, task.workdir),
            session_id=task.sessions.get(role_name) or None,
            log_path=paths.logs_dir(task.id) / log_name,
            title=f"grafeno:{task.id}",
            effort=role.effort,
        )
        started_at = time.monotonic()
        attempt = 0
        try:
            while True:
                result = await driver.run(
                    request,
                    on_event=lambda event: self._on_event(phase, event),
                    on_activity=lambda: self._on_activity(phase),
                )
                self._record_tokens(phase, role, result)
                if result.session_id:
                    task.sessions[role_name] = result.session_id
                    request.session_id = result.session_id  # retry continues the session
                    models.save(task)  # persist: a crash mid-wait keeps the session
                if result.usage_wait is None or attempt >= ratelimit.MAX_ATTEMPTS:
                    break
                attempt += 1
                wait = result.usage_wait or ratelimit.PROBE_SECONDS
                self._set_usage_waiting(True)
                self._info(
                    t("orch.usage_wait.retry",
                      wait=format_duration(wait), attempt=attempt, max=ratelimit.MAX_ATTEMPTS)
                )
                await asyncio.sleep(wait)
        finally:
            self._set_usage_waiting(False)
        self._record_duration(phase, time.monotonic() - started_at)
        if not result.ok:
            if result.usage_wait is not None:
                self._info(t("orch.usage_wait.giving_up", max=ratelimit.MAX_ATTEMPTS))
            self._mark_failed(phase)
            await self._run_hooks(phase, "failed")
            raise PhaseError(result.error or t("orch.phase_failed", phase=phase_label(phase)))
        self.task.failed_phase = ""  # the phase advanced: forget the old failure
        self._set_state(done_state)
        self._info(
            t("orch.phase_done", phase=phase_label(phase), duration=format_duration(time.monotonic() - started_at))
        )
        await self._run_hooks(phase, "ok")
        await self._run_triggers(phase, "after")
        await self._sync_remote_push()
        return result

    def _record_duration(self, phase: str, elapsed: float) -> None:
        durations = self.task.durations
        durations[phase] = int(durations.get(phase, 0) + round(elapsed))
        models.save(self.task)

    def _record_tokens(self, phase: str, role: RoleConfig, result: RunResult) -> None:
        """Accumulate the tokens of the run on the task, by phase and CLI+model."""
        if result.tokens.empty:
            return
        self.task.record_tokens(role.cli, role.model, phase, result.tokens)
        models.save(self.task)

    async def _run_hooks(self, stage: str, outcome: str) -> None:
        """Fire the hooks configured for the stage (never fails)."""
        try:
            await hooks.run_stage_hooks(
                self.task, stage, outcome,
                on_event=self._on_event, on_info=self._info,
            )
        except Exception as exc:  # noqa: BLE001 - hooks never break the pipeline
            self._info(t("hook.exec_error", error=exc))

    async def _run_triggers(self, stage: str, timing: str) -> None:
        """Fire the trigger tasks bound to this stage boundary (never fails)."""
        try:
            triggers.fire(self.task, stage, timing, on_info=self._info)
        except Exception as exc:  # noqa: BLE001 - triggers never break the pipeline
            self._info(t("trig.error", name=stage, error=exc))

    async def _prepare_remote(self) -> None:
        """Ensure the (possibly remote) project is mounted before each phase.

        In session mode tasks without an explicit ``task.remote`` still work
        on remote paths: ``remotesession.spec_for_task`` resolves the session
        spec for them. A mount failure fails the phase: the agents cannot
        work without the project directory.
        """
        spec = remotesession.spec_for_task(self.task)
        if spec is None:
            return
        ok = await remote.ensure_mounted(spec, on_info=self._info)
        if not ok:
            self._set_state(TaskState.FAILED)
            raise PhaseError(t("remote.mount.fail", error=spec.canonical))
        await self._probe_remote_os()

    async def _probe_remote_os(self) -> None:
        """Detect and persist the destination OS of a remote task (once)."""
        task = self.task
        if task.remote_os:
            return  # already probed in a previous phase/run
        session = remotesession.current()
        if not task.is_remote and session is not None:
            # Session task: reuse the OS probed at bootstrap (avoids an
            # extra ssh roundtrip per phase).
            task.remote_os = session.remote_os
            if task.remote_os:
                models.save(task)
            return
        spec = remote.parse_spec(task.remote)
        if spec is None or remote.is_self(spec):
            return
        detected = await remote.detect_os(spec)
        if not detected:
            return
        task.remote_os = detected
        models.save(task)
        self._info(t("remote.os.detected", os=detected))

    async def _sync_remote_push(self) -> None:
        """Mirror task data to the remote host after a phase (best effort)."""
        if not self.task.is_remote:
            return
        try:
            await remote.push_task_for(self.task, on_info=self._info)
        except Exception as exc:  # noqa: BLE001 - sync never breaks the pipeline
            self._info(t("remote.sync.push.fail", error=exc))

    def _write_changes_md(self) -> None:
        """Write final/changes.md with the task diff (best effort, never fails)."""
        try:
            workdir = remote.effective_workdir(self.task.remote, self.task.workdir)
            content = gitops.changes_markdown(workdir, self.task.base_commit)
            if not content:
                return
            changes_path = paths.final_dir(self.task.id, self.task.cycle) / "changes.md"
            changes_path.write_text(content, encoding="utf-8")
            self._info(t("orch.changes_md"))
        except Exception as exc:  # noqa: BLE001 - reporting never breaks the pipeline
            self._info(t("orch.changes_md.fail", error=exc))

    # ------------------------------------------------------------------ #
    # Phases
    # ------------------------------------------------------------------ #
    async def ensure_agents_md(self) -> None:
        """Generate AGENTS.md in the project if missing (best effort).

        Uses the planner role (its CLI and model). A failure here does NOT
        fail the task: it is only reported and continued; the actual
        planner CLI problems are already handled by the plan phase with
        its usual error handling.
        """
        workdir = remote.effective_workdir(self.task.remote, self.task.workdir)
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
            effort=role.effort,
        )
        result = await driver.run(
            request,
            on_event=lambda event: self._on_event("plan", event),
            on_activity=lambda: self._on_activity("plan"),
        )
        self._record_tokens("plan", role, result)
        if result.ok and (workdir / "AGENTS.md").exists():
            self._info(t("orch.agents_md.done"))
        elif result.ok:
            self._info(t("orch.agents_md.no_file"))
        else:
            self._info(t("orch.agents_md.failed", error=result.error or "?"))

    async def run_plan(self) -> None:
        await self._prepare_remote()
        self._set_state(TaskState.PLANNING)  # includes AGENTS.md generation
        await self.ensure_agents_md()
        result = await self._execute(
            "planner",
            "plan",
            prompts.plan_prompt(self.task),
            "plan.jsonl",
            TaskState.PLANNING,
            TaskState.PLANNED,
        )
        self._normalize_md_files(paths.plan_dir(self.task.id, self.task.cycle))
        if not self._plan_files():
            # Fallback: the planner wrote no files; we materialise its output.
            if not result.text.strip():
                self._mark_failed("plan")
                raise PhaseError(t("orch.no_plan_output"))
            plan_path = paths.plan_dir(self.task.id, self.task.cycle) / "01-plan.md"
            plan_path.write_text(
                normalize_markdown(
                    f"{prompts.executor_header(self.task)}\n{prompts.executor_notice(self.task)}\n\n"
                    f"# Plan: {self.task.name}\n\n{result.text.strip()}\n"
                ),
                encoding="utf-8",
            )
            self._info(t("orch.plan_fallback"))

    async def run_reevaluate_plan(self) -> None:
        """Re-evaluate the existing plan (repetitive tasks with plan_reuse=reevaluate).

        Same as ``run_plan`` but without generating AGENTS.md (already
        present) and with the re-evaluation prompt. If there are no plan
        files, it falls back to ``run_plan``.
        """
        await self._prepare_remote()
        if not self._plan_files():
            await self.run_plan()
            return
        result = await self._execute(
            "planner",
            "plan",
            prompts.reevaluate_plan_prompt(self.task),
            "reevaluate.jsonl",
            TaskState.PLANNING,
            TaskState.PLANNED,
        )
        self._normalize_md_files(paths.plan_dir(self.task.id, self.task.cycle))
        if not self._plan_files() and result.text.strip():
            plan_path = paths.plan_dir(self.task.id, self.task.cycle) / "01-plan.md"
            plan_path.write_text(
                normalize_markdown(
                    f"{prompts.executor_header(self.task)}\n{prompts.executor_notice(self.task)}\n\n"
                    f"# Plan: {self.task.name}\n\n{result.text.strip()}\n"
                ),
                encoding="utf-8",
            )

    async def run_implement(self) -> None:
        await self._prepare_remote()
        self._ensure_branch()
        if not self.task.base_commit:
            workdir = remote.effective_workdir(self.task.remote, self.task.workdir)
            head = gitops.current_head(workdir)
            if head:
                self.task.base_commit = head
                models.save(self.task)
        await self._execute(
            "implementer",
            "implement",
            prompts.implement_prompt(self.task),
            "implement.jsonl",
            TaskState.IMPLEMENTING,
            TaskState.IMPLEMENTED,
        )

    async def run_review(self) -> Verdict:
        await self._prepare_remote()
        review_number = self.task.iteration + 1
        result = await self._execute(
            "reviewer",
            "review",
            prompts.review_prompt(self.task, review_number),
            f"review-{review_number:02d}.jsonl",
            TaskState.REVIEWING,
            TaskState.IMPLEMENTED,
        )
        self._normalize_md_files(paths.review_dir(self.task.id, self.task.cycle))
        review_path = paths.review_dir(self.task.id, self.task.cycle) / f"{review_number:02d}-review.md"
        if not review_path.exists() and result.text.strip():
            # Fallback: the reviewer did not write the file; we save its output.
            review_path.write_text(normalize_markdown(result.text), encoding="utf-8")

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
        await self._prepare_remote()
        fix_number = self.task.iteration + 1
        await self._execute(
            "implementer",
            "fix",
            prompts.fix_prompt(self.task, fix_number),
            f"fix-{fix_number:02d}.jsonl",
            TaskState.FIXING,
            TaskState.IMPLEMENTED,
        )
        # Committed only after a successful fix: a failed or paused run
        # retries with the same review file, log name and iteration budget.
        self.task.iteration = fix_number
        models.save(self.task)

    async def run_final(self) -> None:
        await self._prepare_remote()
        result = await self._execute(
            "final",
            "final",
            prompts.final_prompt(self.task),
            "final.jsonl",
            TaskState.FINALIZING,
            TaskState.DONE,
        )
        self._normalize_md_files(paths.final_dir(self.task.id, self.task.cycle))
        final_path = paths.final_dir(self.task.id, self.task.cycle) / "01-final.md"
        if not final_path.exists() and result.text.strip():
            # Fallback: the agent did not write the report; we save its output.
            final_path.write_text(normalize_markdown(result.text), encoding="utf-8")
        self._write_changes_md()

    async def run_tests(self) -> bool:
        await self._prepare_remote()
        command = self.task.test_command.strip()
        if not command:
            return True
        self._info(t("orch.tests.run", command=command))
        await self._run_triggers("tests", "before")
        started_at = time.monotonic()
        session_spec = None
        if not self.task.is_remote:
            session_spec = remotesession.spec_for_task(self.task)
        try:
            if session_spec is not None:
                # Session mode: run the tests ON the remote host (its OS,
                # shell and binaries), streaming output line by line.
                argv = remote.ssh_command(
                    session_spec, f"cd {shlex.quote(self.task.workdir)} && {command}"
                )
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    cwd=remote.effective_workdir(self.task.remote, self.task.workdir),
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
        await self._run_hooks("tests", "ok" if returncode == 0 else "failed")
        await self._run_triggers("tests", "after")
        await self._sync_remote_push()
        return returncode == 0

    # ------------------------------------------------------------------ #
    # Automode
    # ------------------------------------------------------------------ #
    async def run_automode(self) -> None:
        """Full pipeline without pauses: plan -> implementation -> review ⇄ fix."""
        await self.run_automode_plan()
        if self.task.state is TaskState.FAILED:
            return
        await self.run_automode_continue()

    async def run_automode_plan(self) -> None:
        """Only the plan phase (confirmation point when confirm_plan)."""
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

    async def _review_fix_loop(self) -> None:
        """Review/fix cycle shared by run_automode_continue and resume."""
        while self.task.state is not TaskState.DONE:
            await self.run_review()
            if self.task.state is TaskState.DONE:
                break
            if self.task.iteration >= self.task.max_iterations:
                self._mark_failed("review")
                self._info(t("orch.max_iterations", max=self.task.max_iterations))
                return
            await self.run_fix()

    async def run_automode_continue(self) -> None:
        """Implementation + review loop (requires an existing plan)."""
        self.task.automode = True
        models.save(self.task)
        if not self._plan_files():
            self._info(t("orch.no_plan_files"))
            return
        try:
            await self.run_implement()
            await self.run_tests()
            await self._review_fix_loop()
            if self.task.state is TaskState.DONE:
                await self.run_final()
        except PhaseError as exc:
            self._info(str(exc))

    async def run_automode_resume(self) -> None:
        """Resume a FAILED task, reusing the artifacts already on disk."""
        self.task.automode = True
        models.save(self.task)
        failed = self.task.failed_phase
        if failed not in ("implement", "review", "fix", "final"):
            # Legacy/unknown failure ("" or "plan"): full pipeline; the plan
            # phase still reuses any plan files already on disk. The state
            # goes back to DRAFT (artifacts are kept) because run_automode
            # bails out on FAILED tasks.
            if self.task.state is TaskState.FAILED:
                self._set_state(TaskState.DRAFT)
            await self.run_automode()
            return
        self._info(t("orch.resume_from", phase=phase_label(failed)))
        try:
            if failed == "final":
                await self.run_final()
                return
            if self.task.iteration >= self.task.max_iterations:
                # Review/fix budget exhausted: restart it (branch, plan files
                # and other artifacts are still reused).
                self.task.iteration = 0
                models.save(self.task)
            if failed == "implement":
                if not self._plan_files():
                    await self.run_plan()
                await self.run_implement()
                await self.run_tests()
            elif failed == "fix":
                await self.run_fix()
            await self._review_fix_loop()
            if self.task.state is TaskState.DONE:
                await self.run_final()
        except PhaseError as exc:
            self._info(str(exc))

    # ------------------------------------------------------------------ #
    def _ensure_branch(self) -> None:
        task = self.task
        if not task.create_branch or task.branch:
            return
        workdir = remote.effective_workdir(task.remote, task.workdir)
        if not gitops.is_git_repo(workdir):
            self._info(t("orch.not_git"))
            return
        branch = f"grafeno/{models.slugify(task.name)}"
        ok, message = gitops.create_branch(workdir, branch)
        self._info(message)
        if ok:
            task.branch = branch
            models.save(task)


# ---------------------------------------------------------------------- #
def repetition_runner(task: Task) -> Callable[[Orchestrator], Awaitable[None]]:
    """Runner for a repetition according to ``task.plan_reuse``.

    - ``reuse``:      ``run_automode`` (reuses the existing plans).
    - ``replan``:     ``run_automode`` (the caller has already deleted the plans).
    - ``reevaluate``: ``run_reevaluate_plan`` + ``run_automode_continue``.

    A coroutine is returned so that ``TaskRuntime`` runs it just like any
    other pipeline runner.
    """
    if task.plan_reuse == "reevaluate":

        async def _reevaluate(orch: Orchestrator) -> None:
            await orch.run_reevaluate_plan()
            if orch.task.state is TaskState.FAILED:
                return
            await orch.run_automode_continue()

        return _reevaluate

    async def _automode(orch: Orchestrator) -> None:
        await orch.run_automode()

    return _automode
