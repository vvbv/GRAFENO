"""Task detail screen: phases, plans, reviews and live log."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Awaitable, Callable

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from ... import models, paths, remote, scheduler
from ...i18n import t
from ...mdnorm import normalize_markdown
from ...models import Task, TaskState
from ...pipeline.orchestrator import Orchestrator, phase_label
from ...timefmt import format_duration
from ...tokenfmt import format_tokens
from ..widgets import GrafenoHeader, PhaseBar, markdown_set

_SPINNER = "⠋⠙⠹⠸⠼⠴⦦⣾"
_WARN_AFTER_S = 90    # no output: yellow warning
_STALL_AFTER_S = 300  # no output: red warning (possible stall)


class FileItem(ListItem):
    """List item referencing a file on disk."""

    def __init__(self, path: Path, base: Path | None = None):
        label = str(path.relative_to(base)) if base and path.is_relative_to(base) else path.name
        super().__init__(Label(label))
        self.file_path = path


class FileList(ListView):
    """Markdown file list (recursive: includes extension cycles)."""

    def load_dir(self, directory: Path) -> None:
        self.clear()
        for entry in sorted(directory.glob("**/*.md")):
            self.append(FileItem(entry, directory))


class PlanConfirmScreen(ModalScreen[bool]):
    """Automode confirmation point: is the plan OK to implement?"""

    BINDINGS = [Binding("escape", "reject", t("pc.later"))]

    def __init__(self, plan_count: int):
        super().__init__()
        self._plan_count = plan_count

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-confirm-dialog"):
            yield Label(t("pc.title"), id="plan-confirm-title")
            yield Static(
                t("pc.body", count=self._plan_count)
            )
            with Horizontal(id="pc-buttons"):
                yield Button(t("pc.implement"), variant="primary", id="pc-accept")
                yield Button(t("pc.later"), id="pc-reject")

    def action_reject(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pc-accept")


# Description of each phase for the confirmation modal.
_PHASE_INFO = {
    "plan": {"role": "planner"},
    "implement": {"role": "implementer"},
    "review": {"role": "reviewer"},
    "fix": {"role": "implementer"},
    "final": {"role": "final"},
    "tests": {"role": None},
    "automode": {"role": None},
}


class PhaseConfirmScreen(ModalScreen[bool]):
    """Pre-launch phase confirmation: explains what will happen."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, task: Task, phase: str):
        super().__init__()
        self._gtask = task
        self._phase = phase

    def compose(self) -> ComposeResult:
        info = _PHASE_INFO[self._phase]
        title = t(f"phaseinfo.{self._phase}.title")
        what = t(f"phaseinfo.{self._phase}.what")
        with Vertical(id="plan-confirm-dialog"):
            yield Label(t("pconf.question", title=title), id="plan-confirm-title")
            yield Static(what)
            if info["role"]:
                role = self._gtask.role(info["role"])
                yield Static(
                    t("pconf.agent", cli=role.cli, model=role.model or "default"),
                    classes="pc-detail",
                )
            else:
                roles = (
                    t("pconf.role.planner", cli=self._gtask.planner.cli, model=self._gtask.planner.model or "default")
                    + "\n"
                    + t("pconf.role.implementer", cli=self._gtask.implementer.cli, model=self._gtask.implementer.model or "default")
                    + "\n"
                    + t("pconf.role.reviewer", cli=self._gtask.reviewer.cli, model=self._gtask.reviewer.model or "default")
                    + "\n"
                    + t("pconf.role.final", cli=self._gtask.final.cli, model=self._gtask.final.model or "default")
                )
                if self._phase == "automode":
                    yield Static(roles, classes="pc-detail")
            yield Static(t("pconf.project", workdir=self._gtask.workdir), classes="pc-detail")
            if self._phase == "automode" and self._gtask.confirm_plan:
                yield Static(
                    t("pconf.pause_notice"),
                    classes="pc-detail",
                )
            if self._phase == "tests":
                yield Static(
                    t("pconf.command", command=self._gtask.test_command), classes="pc-detail"
                )
            with Horizontal(id="pc-buttons"):
                yield Button(t("pconf.run"), variant="primary", id="pc-accept")
                yield Button(t("common.cancel"), id="pc-reject")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pc-accept")


class StatusConfirmScreen(ModalScreen[bool]):
    """Confirmation of a manual state change (force complete / discard)."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-confirm-dialog"):
            yield Label(self._title, id="plan-confirm-title")
            yield Static(self._body)
            with Horizontal(id="pc-buttons"):
                yield Button(t("det.mark.confirm"), variant="primary", id="pc-accept")
                yield Button(t("common.cancel"), id="pc-reject")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "pc-accept")


class RequestMoreScreen(ModalScreen[str | None]):
    """Ask for an extension: start a new cycle with the same logic."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, task: Task):
        super().__init__()
        self._gtask = task

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label(t("rm.title", cycle=self._gtask.cycle + 1), id="new-task-title")
            yield Label(t("rm.prompt"))
            yield TextArea(id="rm-text")
            body = t(
                "rm.body",
                planner=self._gtask.planner.cli,
                approval=t("rm.approval") if self._gtask.confirm_plan else "",
                implementer=self._gtask.implementer.cli,
                reviewer=self._gtask.reviewer.cli,
            )
            yield Static(body, classes="pc-detail")
            with Horizontal(id="nt-buttons"):
                yield Button(t("rm.accept"), variant="primary", id="rm-accept")
                yield Button(t("common.cancel"), id="rm-cancel")

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
            self.notify(t("rm.error.empty"), severity="error")
            return
        self.dismiss(request)


class EditTaskScreen(ModalScreen[bool]):
    """Edit the task's name and description."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, task: Task):
        super().__init__()
        self._gtask = task

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            yield Label(t("et.title"), id="new-task-title")
            yield Label(t("et.name"))
            yield Input(self._gtask.name, id="et-name")
            yield Label(t("et.description"))
            yield TextArea(id="et-description")
            yield Label(t("et.parent"))
            yield Select([], id="et-parent", allow_blank=True)
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.save"), variant="primary", id="et-save")
                yield Button(t("common.cancel"), id="et-cancel")

    def on_mount(self) -> None:
        self.query_one("#et-description", TextArea).text = self._gtask.description
        self.query_one("#et-name", Input).focus()
        # Parent selector: valid rechain positions plus the current parent
        # (grandfathered even if its position is sealed today).
        all_tasks = models.list_all()
        candidates = scheduler.rechain_candidates(self._gtask, all_tasks)
        options = [(f"{task.name} ({task.id})", task.id) for task in candidates]
        current = self._gtask.parent_id
        if current and all(value != current for _, value in options):
            parent = next((task for task in all_tasks if task.id == current), None)
            label = f"{parent.name} ({parent.id})" if parent else current
            options.append((label, current))
        parent_select = self.query_one("#et-parent", Select)
        parent_select.set_options(options)
        # When current is empty the default Select.NULL is already "no parent";
        # we only assign explicitly when we have a real value to highlight.
        if current:
            parent_select.value = current

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "et-cancel":
            self.dismiss(False)
            return
        name = self.query_one("#et-name", Input).value.strip()
        if not name:
            self.notify(t("et.error.name_required"), severity="error")
            return
        parent_value = self.query_one("#et-parent", Select).value
        # Select.NULL is the sentinel for "no selection"; empty string is the
        # value we store in Task.parent_id when the task has no parent.
        parent_id = "" if parent_value is Select.NULL else str(parent_value)
        if parent_id != self._gtask.parent_id:
            by_id = {task.id: task for task in models.list_all()}
            error_key = scheduler.rechain_error(self._gtask, parent_id, by_id)
            if error_key:
                self.notify(t(error_key), severity="error")
                return
        self._gtask.name = name
        self._gtask.description = self.query_one("#et-description", TextArea).text.strip()
        if parent_id != self._gtask.parent_id:
            self._gtask.parent_id = parent_id
        models.save(self._gtask)
        self.dismiss(True)


class TaskDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("p", "run_plan", t("det.bind.plan")),
        Binding("i", "run_implement", t("det.bind.implement")),
        Binding("r", "run_review", t("det.bind.review")),
        Binding("f", "run_fix", t("det.bind.fix")),
        Binding("s", "run_final", t("det.bind.final")),
        Binding("t", "run_tests", t("det.bind.tests")),
        Binding("a", "run_automode", t("det.bind.automode")),
        Binding("m", "ask_more", t("det.bind.more")),
        Binding("e", "edit_roles", t("det.bind.agents")),
        Binding("E", "edit_info", t("det.bind.edit")),
        Binding("d", "mark_done", t("det.bind.complete")),
        Binding("R", "restart", t("det.bind.restart")),
        Binding("D", "mark_discard", t("det.bind.discard")),
        Binding("x", "cancel", t("det.bind.cancel")),
        Binding("escape", "back", t("common.back")),
    ]

    def __init__(self, task: Task):
        super().__init__()
        self.current_task = task
        self._spinner_index = 0
        self._asking_plan = False

    @property
    def runtime(self):
        """Background runtime of this task (lives in the App)."""
        return self.app.runtime_for(self.current_task)

    # ------------------------------------------------------------------ #
    def compose(self) -> ComposeResult:
        yield GrafenoHeader()
        yield Static("", id="task-title")
        yield PhaseBar(
            self.current_task.state,
            self.current_task.iteration,
            waiting=self.current_task.usage_waiting,
            id="phase-bar",
        )
        yield Static("", id="agents-bar")
        yield Static("", id="activity-bar")
        with TabbedContent(id="tabs"):
            with TabPane(t("det.tab.desc"), id="tab-desc"):
                with VerticalScroll(id="desc-scroll"):
                    yield Static("", id="desc-view")
            with TabPane(t("det.tab.plan"), id="tab-plan"):
                with Horizontal():
                    yield FileList(id="plan-files")
                    with VerticalScroll(id="plan-scroll"):
                        yield Markdown("", id="plan-view")
            with TabPane(t("det.tab.review"), id="tab-review"):
                with Horizontal():
                    yield FileList(id="review-files")
                    with VerticalScroll(id="review-scroll"):
                        yield Markdown("", id="review-view")
            with TabPane(t("det.tab.final"), id="tab-final"):
                with Horizontal():
                    yield FileList(id="final-files")
                    with VerticalScroll(id="final-scroll"):
                        yield Markdown("", id="final-view")
            with TabPane(t("det.tab.log"), id="tab-log"):
                yield RichLog(id="live-log", highlight=False, markup=False, wrap=True)
            with TabPane(t("det.tab.tokens"), id="tab-tokens"):
                with VerticalScroll(id="tokens-scroll"):
                    yield Static("", id="tokens-view")
        yield Footer()

    def on_mount(self) -> None:
        self._render_title()
        self._reload_files()
        runtime = self.runtime
        runtime.add_listener(self._on_runtime)
        if not runtime.log:
            runtime._cb_info(
                t("det.log.header", id=self.current_task.id, cli=self.current_task.implementer.cli, model=self.current_task.implementer.model or "default")
            )
            if self.current_task.branch:
                runtime._cb_info(t("det.log.branch", branch=self.current_task.branch))
        self._replay_log()
        self._render_activity()
        self._render_tokens()
        self._render_agents_bar()
        # 1s clock: the on-screen tick shows the UI isn't frozen.
        self.set_interval(1.0, self._tick)
        self._maybe_plan_confirm()
        # Focusable Markdown viewers: the keyboard (arrows, PgDn...) scrolls.
        for scroll_id in ("#desc-scroll", "#plan-scroll", "#review-scroll", "#final-scroll", "#tokens-scroll"):
            self.query_one(scroll_id, VerticalScroll).can_focus = True
        self.query_one("#desc-view", Static).update(
            self.current_task.description or t("det.desc.empty")
        )
        if self.current_task.is_remote:
            self.run_worker(
                self._pull_remote(),
                exclusive=True,
                group=f"grafeno-pull-{self.current_task.id}",
                exit_on_error=False,
            )

    async def _pull_remote(self) -> None:
        """Fetch newer task data from the remote host (best effort)."""
        await remote.pull_task_for(self.current_task, on_info=self.runtime._cb_info)
        self._reload_files()  # pulled artifacts appear without reopening

    def on_screen_suspend(self) -> None:
        self.runtime.remove_listener(self._on_runtime)

    def on_screen_resume(self) -> None:
        # On return: reconnect with the pipeline that kept running.
        self.runtime.add_listener(self._on_runtime)
        self._replay_log()
        self._state_changed(self.runtime.task)  # repaints PhaseBar, title and files
        self._render_activity()
        self._maybe_plan_confirm()

    # ------------------------------------------------------------------ #
    # Runtime subscription
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
    # Activity bar (heartbeat + watchdog + times)
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        if self.runtime.running:
            self._spinner_index += 1
            self._render_activity()
            self._render_tokens()
            self._render_agents_bar()

    def _total_seconds(self) -> float:
        total = float(self.current_task.total_duration_seconds())
        if self.runtime.phase_started_at is not None:
            total += time.monotonic() - self.runtime.phase_started_at
        return total

    def _render_activity(self) -> None:
        bar = self.query_one("#activity-bar", Static)
        total = format_duration(self._total_seconds())
        runtime = self.runtime
        total_in, total_out = self.current_task.token_totals()
        if not runtime.running or runtime.phase_started_at is None:
            if self.current_task.durations:
                line = Text(t("act.idle_total", total=total), style="dim")
                if total_in or total_out:
                    line.append(
                        f" · {t('det.tokens', input=format_tokens(total_in), output=format_tokens(total_out))}",
                        style="dim",
                    )
                bar.update(line)
            else:
                bar.update(Text(t("act.idle"), style="dim"))
            return

        elapsed = time.monotonic() - runtime.phase_started_at
        silence = time.monotonic() - runtime.last_activity
        spinner = _SPINNER[self._spinner_index % len(_SPINNER)]
        line = Text()
        line.append(f"{spinner} ", style="bold green")
        line.append(f"{runtime.phase_label}", style="bold")
        line.append(f" · {format_duration(elapsed)}", style="green")
        line.append(f" · {t('act.events', count=runtime.event_count)}", style="dim")
        if silence >= _STALL_AFTER_S:
            line.append(
                f" · {t('act.stall', duration=format_duration(silence))}",
                style="bold red",
            )
        elif silence >= _WARN_AFTER_S:
            line.append(
                f" · {t('act.warn', duration=format_duration(silence))}",
                style="yellow",
            )
        else:
            line.append(f" · {t('act.last', duration=format_duration(silence))}", style="dim")
        line.append(f" · {t('act.total', total=total)}", style="dim")
        if total_in or total_out:
            line.append(
                f" · {t('det.tokens', input=format_tokens(total_in), output=format_tokens(total_out))}",
                style="dim",
            )
        bar.update(line)

    def _render_tokens(self) -> None:
        """Render total, breakdown by phase and by CLI+model in the Tokens tab."""
        view = self.query_one("#tokens-view", Static)
        task = self.current_task
        total_in, total_out = task.token_totals()
        if not total_in and not total_out:
            view.update(t("det.tokens.empty"))
            return
        lines: list[str] = [
            t("det.tokens.total", input=format_tokens(total_in), output=format_tokens(total_out)),
            "",
            f"[b]{t('det.tokens.by_phase')}[/b]",
        ]
        by_phase = task.tokens_by_phase()
        ordered = sorted(
            by_phase.items(),
            key=lambda item: (
                models.TOKEN_PHASES.index(item[0])
                if item[0] in models.TOKEN_PHASES
                else len(models.TOKEN_PHASES)
            ),
        )
        for phase, (pair_in, pair_out) in ordered:
            label = t(f"phase.{phase}") if phase != models.LEGACY_PHASE else t("phase.legacy")
            lines.append(f"  {label}: ↑{format_tokens(pair_in)} ↓{format_tokens(pair_out)}")
        lines.append("")
        lines.append(f"[b]{t('det.tokens.by_agent')}[/b]")
        by_agent = task.tokens_by_cli_model()
        for label, (pair_in, pair_out) in sorted(
            by_agent.items(), key=lambda item: (-(item[1][0] + item[1][1]), item[0])
        ):
            lines.append(f"  {label}: ↑{format_tokens(pair_in)} ↓{format_tokens(pair_out)}")
        view.update("\n".join(lines))

    def _render_agents_bar(self) -> None:
        """Under the phase bar: agent (cli/model) and consumption by phase."""
        bar = self.query_one("#agents-bar", Static)
        task = self.current_task
        by_phase = task.tokens_by_phase()
        phases = ["plan", "implement", "review"]
        if "fix" in by_phase:
            phases.append("fix")  # fix uses the implementer role; only if there were fixes
        phases.append("final")
        line = Text()
        for index, phase in enumerate(phases):
            role = task.role(_PHASE_INFO[phase]["role"])
            label = models.cli_model_label(role.cli, role.model or "default")
            line.append(f"{t(f'phase.{phase}')}: ", style="dim")
            line.append(label, style="cyan")
            if phase in by_phase:
                pair_in, pair_out = by_phase[phase]
                line.append(f" ↑{format_tokens(pair_in)} ↓{format_tokens(pair_out)}", style="green")
            if index < len(phases) - 1:
                line.append("  ·  ", style="dim")
        bar.update(line)

    def _log(self) -> RichLog:
        return self.query_one("#live-log", RichLog)

    def _state_changed(self, task: Task) -> None:
        self.current_task = task  # live object from the runtime (orchestrator mutates it)
        self.query_one(PhaseBar).set_state(task.state, task.iteration, waiting=task.usage_waiting)
        self._render_title()
        self._reload_files()  # artifacts appear at the end of each phase
        self._render_tokens()
        self._render_agents_bar()

    def _render_title(self) -> None:
        cycle = f"  [b]·[/b]  {t('det.cycle', n=self.current_task.cycle)}" if self.current_task.cycle > 1 else ""
        extra = ""
        task = self.current_task
        if task.is_remote:
            spec = remote.parse_spec(task.remote)
            target = spec.target if spec else task.remote
            if task.remote_os:
                target = f"{target} ({task.remote_os})"
            extra += f"  [b]·[/b]  {t('det.remote', target=target)}"
        if task.scheduled_at:
            extra += f"  [b]·[/b]  {t('det.scheduled', at=task.scheduled_at.replace('T', ' '))}"
        if task.repeat_mode:
            extra += f"  [b]·[/b]  {t(f'det.repeat.{task.repeat_mode}', n=task.repeat_count, minutes=task.repeat_interval_minutes)}"
        self.query_one("#task-title", Static).update(
            f"[b]{task.name}[/b]{cycle}  [b]·[/b]  {task.workdir}{extra}"
        )

    def _reload_files(self) -> None:
        self.query_one("#plan-files", FileList).load_dir(paths.plan_dir(self.current_task.id))
        self.query_one("#review-files", FileList).load_dir(paths.review_dir(self.current_task.id))
        self.query_one("#final-files", FileList).load_dir(paths.final_dir(self.current_task.id))

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id
        views = {"plan-files": "#plan-view", "review-files": "#review-view", "final-files": "#final-view"}
        if list_id not in views or not isinstance(event.item, FileItem):
            return
        target = event.item.file_path
        view_id = views[list_id]
        try:
            text = normalize_markdown(target.read_text(encoding="utf-8"))
        except OSError as exc:
            self.notify(t("det.error.read", name=target.name, error=exc), severity="error")
            return
        await markdown_set(self.query_one(view_id, Markdown), text)

    # ------------------------------------------------------------------ #
    # Pipeline execution (through the App's runtime)
    # ------------------------------------------------------------------ #
    def _start(
        self,
        runner: Callable[[Orchestrator], Awaitable[None]],
        label: str,
        plan_then_ask: bool = False,
    ) -> None:
        from ...drivers import available_clis

        if not available_clis():
            self.notify(t("det.no_clis"), severity="error")
            return
        if not self.runtime.start(self.app, runner, label, plan_then_ask=plan_then_ask):
            self.notify(t("det.warn.running"), severity="warning")
            return
        # Immediate refresh: the modal's callback arrives with the screen
        # suspended and the first state event may be lost.
        self._state_changed(self.runtime.task)
        self._render_activity()

    # ------------------------------------------------------------------ #
    # Actions (all go through the confirmation modal)
    # ------------------------------------------------------------------ #
    def _confirm(
        self,
        phase: str,
        runner: Callable[[Orchestrator], Awaitable[None]],
        label: str,
        plan_then_ask: bool = False,
    ) -> None:
        if self.runtime.running:
            self.notify(t("det.warn.running"), severity="warning")
            return
        if self.current_task.state is TaskState.DISCARDED:
            self.notify(t("det.warn.discarded"), severity="warning")
            return

        def decide(accepted: bool) -> None:
            if accepted:
                self._start(runner, label, plan_then_ask)

        self.app.push_screen(PhaseConfirmScreen(self.current_task, phase), decide)

    def action_run_plan(self) -> None:
        self._confirm("plan", lambda orch: orch.run_plan(), phase_label("plan"))

    def action_run_implement(self) -> None:
        if not list(paths.plan_dir(self.current_task.id, self.current_task.cycle).glob("*.md")):
            self.notify(t("det.warn.need_plan"), severity="warning")
            return
        self._confirm("implement", lambda orch: orch.run_implement(), phase_label("implement"))

    def action_run_review(self) -> None:
        if self.current_task.state not in {TaskState.IMPLEMENTED, TaskState.PAUSED, TaskState.FAILED}:
            self.notify(t("det.warn.need_impl"), severity="warning")
            return
        self._confirm("review", lambda orch: orch.run_review(), phase_label("review"))

    def action_run_fix(self) -> None:
        if self.current_task.iteration == 0 and not list(paths.review_dir(self.current_task.id, self.current_task.cycle).glob("*.md")):
            self.notify(t("det.warn.need_review"), severity="warning")
            return
        self._confirm("fix", lambda orch: orch.run_fix(), phase_label("fix"))

    def action_run_final(self) -> None:
        if self.current_task.state is not TaskState.DONE:
            self.notify(t("det.warn.need_done"), severity="warning")
            return
        self._confirm("final", lambda orch: orch.run_final(), phase_label("final"))

    def action_run_tests(self) -> None:
        if not self.current_task.test_command.strip():
            self.notify(t("det.warn.no_tests"), severity="warning")
            return

        async def _tests(orch: Orchestrator) -> None:
            ok = await orch.run_tests()
            self.runtime._cb_info(t("det.tests.ok") if ok else t("det.tests.fail"))

        self._confirm("tests", _tests, phase_label("tests"))

    def _pipeline_runner(self) -> tuple[Callable[[Orchestrator], Awaitable[None]], str, bool]:
        """Full pipeline runner respecting confirm_plan (same logic for the
        first cycle and for the extensions)."""
        if self.current_task.confirm_plan:
            return (lambda orch: orch.run_automode_plan()), t("det.cycle_plan", n=self.current_task.cycle), True
        return (lambda orch: orch.run_automode()), t("det.cycle_auto", n=self.current_task.cycle), False

    def action_run_automode(self) -> None:
        runner, label, plan_then_ask = self._pipeline_runner()
        self._confirm("automode", runner, label, plan_then_ask)

    def action_ask_more(self) -> None:
        if self.runtime.running:
            self.notify(t("det.warn.running"), severity="warning")
            return
        if self.current_task.state is TaskState.DISCARDED:
            self.notify(t("det.warn.discarded"), severity="warning")
            return

        def accepted(request: str | None) -> None:
            if not request:
                return
            self.current_task.start_new_cycle(request)
            models.save(self.current_task)
            self.runtime.task = self.current_task
            self._state_changed(self.current_task)
            self.runtime._cb_info(t("det.cycle_started", n=self.current_task.cycle, request=request))
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
                    t("det.automode_impl"),
                )
            else:
                self.runtime._cb_info(
                    t("det.paused_hint")
                )

        self.app.push_screen(PlanConfirmScreen(plan_count), answered)

    def action_cancel(self) -> None:
        if self.runtime.running:
            self.runtime.cancel()
            self.notify(t("det.cancel"))

    def action_restart(self) -> None:
        if self.current_task.state is TaskState.DISCARDED:
            self.notify(t("det.warn.discarded"), severity="warning")
            return

        def decide(accepted: bool) -> None:
            if accepted:
                self.run_worker(
                    self._do_restart(),
                    exclusive=True,
                    group=f"grafeno-restart-{self.current_task.id}",
                )

        self.app.push_screen(
            StatusConfirmScreen(t("det.restart.title"), t("det.restart.body")),
            decide,
        )

    async def _do_restart(self) -> None:
        """Abort the running execution (if any) and reset to DRAFT."""
        if self.runtime.running:
            self.runtime.cancel()
            # Wait for the worker to die: _wrap sets running=False when it
            # catches CancelledError (intermediate PAUSED state).
            for _ in range(100):  # 100 x 0.1s = 10s budget
                await asyncio.sleep(0.1)
                if not self.runtime.running:
                    break
            if self.runtime.running:
                self.notify(t("det.warn.running"), severity="warning")
                return
        models.reset_to_draft(self.current_task)
        self.runtime.task = self.current_task
        self.runtime.log.clear()
        self._state_changed(self.current_task)
        self._render_activity()
        self.runtime._cb_info(t("det.restarted"))

    def action_edit_info(self) -> None:
        if self.runtime.running:
            self.notify(t("det.warn.running"), severity="warning")
            return
        previous_parent = self.current_task.parent_id

        def closed(saved: bool) -> None:
            if not saved:
                return
            # The runtime uses the in-memory copy: refresh it.
            self.runtime.task = self.current_task
            self._render_title()
            self.query_one("#desc-view", Static).update(
                self.current_task.description or t("det.desc.empty")
            )
            self.runtime._cb_info(t("det.info_updated", name=self.current_task.name))
            if self.current_task.parent_id != previous_parent:
                if self.current_task.parent_id:
                    try:
                        parent = models.load(self.current_task.parent_id)
                        name = parent.name
                    except Exception:
                        name = self.current_task.parent_id
                    self.runtime._cb_info(t("et.rechained", name=name))
                else:
                    self.runtime._cb_info(t("et.unchained"))

        self.app.push_screen(EditTaskScreen(self.current_task), closed)

    def action_edit_roles(self) -> None:
        if self.runtime.running:
            self.notify(t("det.warn.running"), severity="warning")
            return

        def closed(saved: bool) -> None:
            if not saved:
                return
            # The runtime uses the in-memory copy: refresh it so the next
            # phase uses the new CLI/model.
            self.runtime.task = self.current_task
            self._render_title()
            self._render_agents_bar()
            self.runtime._cb_info(
                t(
                    "det.agents_updated",
                    planner_cli=self.current_task.planner.cli,
                    planner_model=self.current_task.planner.model or "default",
                    implementer_cli=self.current_task.implementer.cli,
                    implementer_model=self.current_task.implementer.model or "default",
                    reviewer_cli=self.current_task.reviewer.cli,
                    reviewer_model=self.current_task.reviewer.model or "default",
                )
            )

        from .roles import TaskRolesScreen

        self.app.push_screen(TaskRolesScreen(self.current_task), closed)

    def action_back(self) -> None:
        # Going back never interrupts: the pipeline keeps running in the background.
        self.dismiss()

    # ------------------------------------------------------------------ #
    # Manual state change (force complete / discard)
    # ------------------------------------------------------------------ #
    def _mark_state(
        self,
        state: TaskState,
        title_key: str,
        body_key: str,
        log_key: str,
    ) -> None:
        """Change the task state manually, with confirmation."""
        if self.runtime.running:
            self.notify(t("det.warn.running"), severity="warning")
            return
        if self.current_task.state is state:
            key = "det.warn.already_done" if state is TaskState.DONE else "det.warn.already_discarded"
            self.notify(t(key), severity="warning")
            return
        if self.current_task.state is TaskState.DISCARDED:
            self.notify(t("det.warn.discarded"), severity="warning")
            return

        def decide(accepted: bool) -> None:
            if not accepted:
                return
            self.current_task.state = state
            models.save(self.current_task)
            self.runtime.task = self.current_task
            self._state_changed(self.current_task)
            self.runtime._cb_info(t(log_key))

        self.app.push_screen(
            StatusConfirmScreen(t(title_key), t(body_key)), decide
        )

    def action_mark_done(self) -> None:
        self._mark_state(
            TaskState.DONE,
            "det.mark.done.title",
            "det.mark.done.body",
            "det.marked.done",
        )

    def action_mark_discard(self) -> None:
        self._mark_state(
            TaskState.DISCARDED,
            "det.mark.discard.title",
            "det.mark.discard.body",
            "det.marked.discarded",
        )
