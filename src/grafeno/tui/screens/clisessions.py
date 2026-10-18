"""Direct CLI sessions screen: talk to Claude Code or OpenCode in a project folder.

Lists the sessions (of one project or all of them) and shows the selected
conversation. Turns run in the background through ``App.cli_sessions`` so
leaving the screen never stops them; the screen only mirrors the transcript
stored on disk and refreshes it on manager events.
"""

from __future__ import annotations

import os

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, RichLog, Static

from ... import clisessions, config as config_module, remotesession
from ...clisession_runtime import SessionBusy, SessionClosed
from ...clisessions import SESSION_CLIS, STATE_CLOSED, CliSession
from ...drivers import fetch_all_models, fetch_all_variants
from ...drivers.base import EventKind, RunEvent
from ...i18n import t
from ...tokenfmt import format_tokens
from ..dirpicker import DirectoryPicker
from ..rolesform import RolesForm
from ..runtime import format_event
from ..widgets import GrafenoHeader, LocationBar
from .detail import StatusConfirmScreen

SESSION_ROLES: tuple[tuple[str, str], ...] = (("session", "cs.role"),)


class NewCliSessionScreen(ModalScreen["dict | None"]):
    """Form of a new session: folder, name, agent (CLI + model + effort), first prompt."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, workdir: str = "") -> None:
        super().__init__()
        self._workdir = workdir

    def compose(self) -> ComposeResult:
        with Vertical(id="cs-new-dialog"):
            yield Label(t("cs.new.title"), id="new-task-title")
            yield Label(t("cs.field.workdir"))
            yield DirectoryPicker(self._workdir or os.getcwd(), input_id="cs-new-workdir")
            yield Label(t("cs.field.name"))
            yield Input(id="cs-new-name")
            yield RolesForm(SESSION_ROLES, clis=SESSION_CLIS, id="cs-new-roles")
            yield Static("", id="cs-new-status", markup=False)
            yield Label(t("cs.field.prompt"))
            yield Input(id="cs-new-prompt")
            with Horizontal(id="nt-buttons"):
                yield Button(t("cs.new"), variant="primary", id="cs-new-ok")
                yield Button(t("common.cancel"), id="cs-new-cancel")

    def on_mount(self) -> None:
        role = config_module.load().role("implementer")
        cli, model, effort = (
            (role.cli, role.model, role.effort) if role.cli in SESSION_CLIS else ("claude", "", "")
        )
        self.query_one(RolesForm).set_role("session", cli, model, effort)
        self.run_worker(self._load_models, exclusive=True, group="cs-new-models", exit_on_error=False)

    async def _load_models(self) -> None:
        models_map = await fetch_all_models(SESSION_CLIS)
        variants_map = await fetch_all_variants(SESSION_CLIS)
        form = self.query_one(RolesForm)
        form.set_models(models_map)
        form.set_variants(variants_map)
        self.query_one("#cs-new-status", Static).update(" · ".join(
            t("cfg.models.count", cli=cli, count=len(models))
            if models else t("cfg.models.unavailable", cli=cli)
            for cli, models in models_map.items()
        ))

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id != "cs-new-ok":
            self.dismiss(None)
            return
        cli, model, effort = self.query_one(RolesForm).role_values("session")
        self.dismiss({
            "workdir": self.query_one("#cs-new-workdir", Input).value.strip(),
            "name": self.query_one("#cs-new-name", Input).value.strip(),
            "cli": cli,
            "model": model,
            "effort": effort,
            "prompt": self.query_one("#cs-new-prompt", Input).value.strip(),
        })


class _RenameScreen(ModalScreen["str | None"]):
    """Minimal modal with one Input holding the session name."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def compose(self) -> ComposeResult:
        with Vertical(id="cs-new-dialog"):
            yield Label(t("cs.rename"), id="new-task-title")
            yield Input(self._name, id="cs-rename-input")
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.save"), variant="primary", id="cs-rename-ok")
                yield Button(t("common.cancel"), id="cs-rename-cancel")

    def on_mount(self) -> None:
        self.query_one("#cs-rename-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "cs-rename-ok":
            self.dismiss(self.query_one("#cs-rename-input", Input).value.strip() or None)
        else:
            self.dismiss(None)


class CliSessionsScreen(Screen[None]):
    """Sessions of one project (``workdir``) or of every project (``None``)."""

    BINDINGS = [
        Binding("escape", "back", t("common.back")),
        Binding("n", "new_session", t("cs.new")),
        Binding("ctrl+x", "stop_turn", t("cs.stop")),
        Binding("e", "rename", t("cs.rename")),
        Binding("c", "toggle_closed", t("cs.close")),
        Binding("delete", "delete_session", t("cs.delete")),
        Binding("r", "reload", t("tasks.bind.reload")),
    ]

    def __init__(self, workdir: str | None = None) -> None:
        super().__init__()
        self._workdir = workdir
        self._sessions: list[CliSession] = []
        self._selected: str | None = None
        self._shown_entries = 0  # transcript entries already written to the log
        self._placeholder_shown = False  # the empty-transcript notice is in the log

    # ------------------------------------------------------------ layout #
    def compose(self) -> ComposeResult:
        if self._workdir is None:
            title = t("cs.title")
        else:
            title = t("cs.title.project", name=os.path.basename(self._workdir.rstrip(os.sep)) or self._workdir)
        yield GrafenoHeader()
        yield LocationBar(id="location-bar")
        yield Static(title, id="cs-subtitle", markup=False)
        with Horizontal(id="cs-body"):
            with Vertical(id="cs-left"):
                yield DataTable(id="cs-table", cursor_type="row", zebra_stripes=True)
                with Horizontal(id="cs-left-buttons"):
                    yield Button(t("cs.new"), id="cs-new", compact=True)
            with Vertical(id="cs-right"):
                yield Static("", id="cs-head", markup=False)
                yield RichLog(id="cs-log", highlight=False, markup=False, wrap=True)
                yield Static("", id="cs-hint", markup=False)
                with Horizontal(id="cs-compose"):
                    yield Input(placeholder=t("cs.prompt.placeholder"), id="cs-input")
                    yield Button(t("cs.send"), id="cs-send", variant="primary", compact=True)
                    yield Button(t("cs.stop"), id="cs-stop", compact=True)
                    yield Button(t("cs.rename"), id="cs-rename", compact=True)
                    yield Button(t("cs.close"), id="cs-toggle", compact=True)
                    yield Button(t("cs.delete"), id="cs-delete", variant="error", compact=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#cs-table", DataTable)
        columns = [t("cs.col.name")]
        if self._workdir is None:
            columns.append(t("cs.col.project"))
        columns += [t("cs.col.agent"), t("cs.col.state"), t("cs.col.turns"), t("cs.col.updated")]
        table.add_columns(*columns)
        self.app.cli_sessions.add_listener(self._on_session)
        self._reload()
        if self._sessions:
            self._select(self._sessions[0].id)
        else:
            self._render_head()
            self._render_controls()
        table.focus()

    def on_unmount(self) -> None:
        self.app.cli_sessions.remove_listener(self._on_session)

    # -------------------------------------------------------------- data #
    @property
    def _manager(self):
        return self.app.cli_sessions

    def _current(self) -> CliSession | None:
        if self._selected is None:
            return None
        try:
            return self._manager.get(self._selected)
        except (FileNotFoundError, OSError):
            return None

    def _state_label(self, session: CliSession) -> str:
        if self._manager.is_running(session.id):
            return t("cs.state.running")
        return t(f"cs.state.{session.state}")

    def _reload(self) -> None:
        """Re-read the sessions and refill the table keeping the selection."""
        self._sessions = clisessions.list_all(self._workdir or "")
        table = self.query_one("#cs-table", DataTable)
        table.clear()
        for index, session in enumerate(self._sessions):
            cells = [Text(session.name)]
            if self._workdir is None:
                cells.append(Text(session.project_name))
            cells += [
                Text(f"{session.cli} {session.model}".strip()),
                Text(self._state_label(session)),
                Text(str(session.turns)),
                Text(session.updated_at.replace("T", " ")),
            ]
            table.add_row(*cells, key=session.id)
            if session.id == self._selected:
                table.move_cursor(row=index, scroll=False)

    # -------------------------------------------------------- selection #
    def _select(self, session_id: str) -> None:
        self._selected = session_id
        self.query_one("#cs-log", RichLog).clear()
        self._shown_entries = 0
        self._placeholder_shown = False
        self._append_new_entries()
        self._render_head()
        self._render_controls()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key is not None else None
        if key and key != self._selected:
            self._select(key)

    def _entry_text(self, entry: dict) -> Text:
        text = str(entry.get("text", ""))
        if entry.get("kind") == "user":
            return Text(f"> {text}", style="bold green")
        try:
            kind = EventKind(entry.get("kind"))
        except ValueError:
            kind = EventKind.INFO
        return format_event(RunEvent(kind, text))

    def _append_new_entries(self) -> None:
        """Write the transcript entries not shown yet (disk is the source of truth)."""
        if self._selected is None:
            return
        entries, total = clisessions.load_transcript(self._selected, after=self._shown_entries)
        log = self.query_one("#cs-log", RichLog)
        if entries and self._placeholder_shown:
            log.clear()  # first real entries replace the notice
            self._placeholder_shown = False
        for entry in entries:
            log.write(self._entry_text(entry))
        if total == 0:
            if not self._placeholder_shown:
                log.write(Text(t("cs.empty.transcript"), style="dim"))
                self._placeholder_shown = True
            return
        self._shown_entries = total

    def _render_head(self) -> None:
        head = self.query_one("#cs-head", Static)
        session = self._current()
        if session is None:
            head.update(
                t("cs.empty.all" if self._workdir is None else "cs.empty") if not self._sessions else t("cs.select")
            )
            return
        tokens = f"{format_tokens(session.tokens_in)}/{format_tokens(session.tokens_out)}"
        head.update(" - ".join((
            session.name,
            f"{session.cli} {session.model}".strip(),
            self._state_label(session),
            t("cs.tokens", tokens=tokens, turns=session.turns),
        )))

    def _render_controls(self) -> None:
        session = self._current()
        remote = remotesession.active()
        running = session is not None and self._manager.is_running(session.id)
        closed = session is not None and session.state == STATE_CLOSED
        self.query_one("#cs-new", Button).disabled = remote
        self.query_one("#cs-input", Input).disabled = session is None or closed
        self.query_one("#cs-send", Button).disabled = session is None or closed or running
        self.query_one("#cs-stop", Button).disabled = not running
        for button_id in ("#cs-rename", "#cs-toggle", "#cs-delete"):
            self.query_one(button_id, Button).disabled = session is None
        self.query_one("#cs-toggle", Button).label = t("cs.reopen" if closed else "cs.close")
        hint = self.query_one("#cs-hint", Static)
        if remote:
            hint.update(t("cs.unavailable.remote"))
        elif closed:
            hint.update(t("cs.closed_notice"))
        elif session is not None:
            hint.update(t("cs.hint.auto", workdir=session.workdir))
        else:
            hint.update("")

    # ----------------------------------------------------------- events #
    def _on_session(self, kind: str, session: CliSession, entry: dict | None) -> None:
        """Manager listener: mirror changes (the screen may be unmounting)."""
        try:
            if kind == "deleted":
                if session.id == self._selected:
                    self._selected = None
                    self.query_one("#cs-log", RichLog).clear()
                    self._placeholder_shown = False
                self._reload()
                self._render_head()
                self._render_controls()
            elif kind == "entry":
                if session.id == self._selected:
                    self._append_new_entries()
            else:
                self._reload()
                if session.id == self._selected:
                    self._append_new_entries()
                    self._render_head()
                    self._render_controls()
        except Exception:  # noqa: BLE001 - NoMatches while the screen unmounts
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "cs-input":
            self._send()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        handlers = {
            "cs-new": self.action_new_session,
            "cs-send": self._send,
            "cs-stop": self.action_stop_turn,
            "cs-rename": self.action_rename,
            "cs-toggle": self.action_toggle_closed,
            "cs-delete": self.action_delete_session,
        }
        handler = handlers.get(event.button.id or "")
        if handler is not None:
            event.stop()
            handler()

    # ---------------------------------------------------------- actions #
    def _send(self) -> None:
        field = self.query_one("#cs-input", Input)
        text = field.value.strip()
        if not text:
            self.notify(t("cs.error.empty_prompt"), severity="warning")
            return
        if self._selected is None:
            return
        try:
            self._manager.send(self._selected, text)
        except SessionBusy:
            self.notify(t("cs.busy"), severity="warning")
        except SessionClosed:
            self.notify(t("cs.closed_notice"), severity="warning")
        except (ValueError, FileNotFoundError) as exc:
            self.notify(str(exc), severity="error")
        else:
            field.value = ""

    def action_new_session(self) -> None:
        if remotesession.active():
            return

        def created(values: dict | None) -> None:
            if not values:
                return
            try:
                session = self._manager.create(
                    values["workdir"], values["cli"], model=values["model"],
                    effort=values["effort"], name=values["name"],
                )
            except ValueError as exc:
                self.notify(t("cs.error.workdir", error=exc), severity="error")
                return
            if values["prompt"]:
                try:
                    self._manager.send(session.id, values["prompt"])
                except (ValueError, SessionBusy, SessionClosed) as exc:
                    self.notify(str(exc), severity="error")
            self._reload()
            self._select(session.id)
            self.notify(t("cs.created"))

        self.app.push_screen(NewCliSessionScreen(self._workdir or ""), created)

    def action_stop_turn(self) -> None:
        if self._selected is not None:
            self._manager.cancel(self._selected)

    def action_rename(self) -> None:
        session = self._current()
        if session is None:
            return

        def renamed(name: str | None) -> None:
            if name and self._selected is not None:
                try:
                    self._manager.rename(self._selected, name)
                except (ValueError, FileNotFoundError) as exc:
                    self.notify(str(exc), severity="error")

        self.app.push_screen(_RenameScreen(session.name), renamed)

    def action_toggle_closed(self) -> None:
        session = self._current()
        if session is None:
            return
        try:
            if session.state == STATE_CLOSED:
                self._manager.reopen(session.id)
            else:
                self._manager.close(session.id)
        except SessionBusy:
            self.notify(t("cs.busy"), severity="warning")

    def action_delete_session(self) -> None:
        session = self._current()
        if session is None:
            return
        session_id = session.id

        def confirmed(ok: bool | None) -> None:
            if ok:
                # App-level worker: deletion must finish even if the user leaves the screen.
                self.app.run_worker(self._manager.delete(session_id), exit_on_error=False)
                self.notify(t("cs.deleted"))

        self.app.push_screen(
            StatusConfirmScreen(t("cs.delete"), t("cs.delete.confirm", name=session.name)),
            confirmed,
        )

    def action_reload(self) -> None:
        self._reload()
        self._render_head()
        self._render_controls()

    def action_back(self) -> None:
        self.dismiss(None)
