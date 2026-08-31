"""Consoles screen: project shell tabs with rename and color support."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from rich.ansi import AnsiDecoder
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Input, Label, RichLog, Select, Static

from ... import consoles
from ...consoles import CONSOLE_COLORS, ConsoleSpec
from ...i18n import t
from ..console_pty import ConsoleProcess
from ..widgets import GrafenoHeader, LocationBar

_MAX_LOG_LINES = 5000  # per-console transcript bound

# Alternate-screen escape sequences (full-screen TUIs cannot be rendered by
# the line-oriented RichLog).
_ALT_ENTER = (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h")
_ALT_EXIT = (b"\x1b[?1049l", b"\x1b[?1047l", b"\x1b[?47l")
_SCAN_TAIL = 15  # bytes kept across reads so split markers still match


class ConsoleView(Vertical):
    """Output area of one console session (its own RichLog transcript)."""

    def __init__(self, view_id: str, index: int):
        # NOTE: the id is intentionally NOT tied to the tab position. Tabs can
        # be added and removed at any index, and re-keying the views would
        # force a full unmount/remount every time (racy with Textual's mount
        # queue). A monotonic counter (managed by ``ConsolesScreen``) keeps
        # every view id unique for the lifetime of the screen without shifting.
        super().__init__(id=view_id)
        self.index = index
        # ``Widget.log`` is a read-only property in Textual, so use a distinct
        # name for the per-view transcript widget.
        self.output: RichLog = RichLog(
            max_lines=_MAX_LOG_LINES, wrap=True, highlight=False, markup=False,
        )

    def compose(self) -> ComposeResult:
        yield self.output


@dataclass
class _Session:
    """Runtime state of one console tab (process, view and ANSI decoding)."""

    proc: ConsoleProcess
    view: ConsoleView
    decoder: AnsiDecoder = field(default_factory=AnsiDecoder)
    buf: str = ""  # decoded text waiting for a newline
    scan_tail: bytes = b""       # tail of the previous chunk (marker scanning)
    fullscreen: bool = False     # the process is in the alternate screen
    fullscreen_notified: bool = False  # the notice was already shown once


class ConsoleFormScreen(ModalScreen["ConsoleSpec | None"]):
    """Create/edit form of a console: name, command and color."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, spec: ConsoleSpec | None = None):
        super().__init__()
        self._spec = spec

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog"):
            title = t("consoles.form.title.edit" if self._spec else "consoles.form.title.new")
            yield Label(title, id="new-task-title")
            yield Label(t("consoles.form.name"))
            yield Input(self._spec.name if self._spec else "", id="cf-name")
            yield Label(t("consoles.form.command"))
            yield Input(self._spec.command if self._spec else "", id="cf-command")
            yield Label(t("consoles.form.color"))
            options = [(t("consoles.color.default"), "")]
            options += [(t(f"consoles.color.{c}"), c) for c in CONSOLE_COLORS if c]
            yield Select(
                options,
                id="cf-color",
                value=self._spec.color if self._spec else "",
                allow_blank=False,
            )
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.save"), variant="primary", id="cf-save")
                yield Button(t("common.cancel"), id="cf-cancel")

    def on_mount(self) -> None:
        self.query_one("#cf-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cf-cancel":
            self.dismiss(None)
            return
        name = self.query_one("#cf-name", Input).value.strip()
        if not name:
            self.notify(t("consoles.error.name_required"), severity="error")
            return
        self.dismiss(ConsoleSpec(
            name=name,
            command=self.query_one("#cf-command", Input).value.strip(),
            color=str(self.query_one("#cf-color", Select).value),
        ))


class ConsolesScreen(Screen[None]):
    """Project consoles: tabs with a live shell each, colored and renamed."""

    BINDINGS = [Binding("escape", "back", t("common.back"))]

    def __init__(self, workdir: Path):
        super().__init__()
        self._workdir = workdir
        self._specs: list[ConsoleSpec] = consoles.load_project(workdir)
        self._sessions: dict[int, _Session] = {}
        self._active: int | None = None
        # Monotonic id counter for ConsoleView widgets. Decoupling the view id
        # from the tab index avoids DuplicateIds when tabs are added or removed
        # in the middle of the list (see _delete_active).
        self._view_counter: int = 0

    def compose(self) -> ComposeResult:
        yield GrafenoHeader()
        yield LocationBar(id="location-bar")
        yield Static(t("consoles.title", path=str(self._workdir)), id="consoles-title")
        yield Horizontal(id="console-tabs")
        with Horizontal(id="console-actions"):
            yield Button(t("consoles.new"), id="con-new", compact=True)
            yield Button(t("consoles.edit"), id="con-edit", compact=True)
            yield Button(t("consoles.delete"), id="con-delete", compact=True)
            yield Button(t("consoles.interrupt"), id="con-interrupt", compact=True)
            yield Button(t("consoles.terminal"), id="con-terminal", compact=True)
        with Vertical(id="console-frame"):
            yield Vertical(id="console-views")
            yield Input(placeholder=t("consoles.input.placeholder"), id="console-input")
        yield Static("", id="console-empty")
        yield Footer()

    async def on_mount(self) -> None:
        self._render_state()
        if consoles.supported() and self._specs:
            await self._activate(0)

    async def on_unmount(self) -> None:
        for index in list(self._sessions):
            await self._teardown_session(index)

    def action_back(self) -> None:
        self.dismiss()

    # ------------------------------------------------------------------ #
    # State rendering
    # ------------------------------------------------------------------ #
    def _render_state(self) -> None:
        """Show the empty/unsupported hint or the console frame."""
        empty = self.query_one("#console-empty", Static)
        frame = self.query_one("#console-frame", Vertical)
        actions = self.query_one("#console-actions", Horizontal)
        if not consoles.supported():
            empty.update(t("consoles.unsupported"))
            empty.display = True
            frame.display = False
            actions.display = False
            return
        if not self._specs:
            empty.update(t("consoles.empty"))
            empty.display = True
            frame.display = False
            actions.display = True  # New stays available
            return
        empty.display = False
        frame.display = True
        actions.display = True

    async def _refresh_tabs(self) -> None:
        """Rebuild the tab bar from scratch (no stale inline styles)."""
        bar = self.query_one("#console-tabs", Horizontal)
        # ``remove_children`` is async (returns AwaitRemove): must be awaited
        # so the children are gone BEFORE we mount the new buttons, otherwise
        # the new IDs collide with the existing ones.
        await bar.remove_children()
        buttons = []
        for index, spec in enumerate(self._specs):
            button = Button(spec.name, id=f"con-tab-{index}", classes="console-tab", compact=True)
            if spec.color:
                button.styles.background = spec.color
                button.styles.color = "black"
            if index == self._active:
                button.styles.text_style = "bold"
            buttons.append(button)
        if buttons:
            await bar.mount(*buttons)

    def _apply_frame_color(self) -> None:
        """Tint the console frame border with the active console color."""
        frame = self.query_one("#console-frame", Vertical)
        color = self._specs[self._active].color if self._active is not None else ""
        frame.styles.border = ("round", color or "#30363d")

    def _persist(self) -> None:
        """Save the console definitions (best effort: never breaks the UI)."""
        try:
            consoles.save_project(self._workdir, self._specs)
        except OSError as exc:
            self.notify(t("consoles.error.save", error=exc), severity="error")

    # ------------------------------------------------------------------ #
    # Sessions (spawn / teardown / activation)
    # ------------------------------------------------------------------ #
    def _spawn(self, index: int) -> _Session:
        spec = self._specs[index]
        self._view_counter += 1
        view = ConsoleView(f"con-view-{self._view_counter - 1}", index)
        self.query_one("#console-views", Vertical).mount(view)
        proc = ConsoleProcess(spec.command, str(self._workdir))
        session = _Session(proc=proc, view=view)
        self._sessions[index] = session
        try:
            proc.start()
        except OSError as exc:
            view.output.write(Text(t("consoles.error.spawn", error=exc), style="bold red"))
            return session
        assert proc.fd is not None
        loop = asyncio.get_running_loop()
        loop.add_reader(proc.fd, partial(self._on_readable, index))
        return session

    async def _teardown_session(self, index: int) -> None:
        session = self._sessions.pop(index, None)
        if session is None:
            return
        fd = session.proc.fd
        if fd is not None:
            asyncio.get_running_loop().remove_reader(fd)
        session.proc.close()
        await session.view.remove()

    async def _activate(self, index: int) -> None:
        """Select a tab, spawning (or respawning) its process if needed."""
        if not 0 <= index < len(self._specs):
            return
        self._active = index
        session = self._sessions.get(index)
        if session is not None and not session.proc.running:
            await self._teardown_session(index)  # dead process: reopen fresh
            session = None
        if session is None:
            session = self._spawn(index)
        for other, open_session in self._sessions.items():
            open_session.view.display = other == index
        self._render_state()
        await self._refresh_tabs()
        self._apply_frame_color()
        self.query_one("#console-input", Input).focus()

    def _on_readable(self, index: int) -> None:
        """fd readable: drain output; on EOF announce the exit."""
        session = self._sessions.get(index)
        if session is None:
            return
        data = session.proc.read()
        if data:
            # Split the chunk at the first alt-screen transition so any bytes
            # BEFORE the marker are rendered under the old state and any bytes
            # AFTER it (typically escape soup) are routed under the new state.
            was_fullscreen = session.fullscreen
            split, _entered, _exited = self._scan_fullscreen(session, data)
            # The notice is state-based: emit whenever we are NOW fullscreen
            # and have not already done so this session. Doing it here (not
            # inside the marker-split branch) covers markers split across
            # reads, where the marker lives entirely in the rolling
            # ``scan_tail`` and ``split`` is 0 even though the state flipped.
            if session.fullscreen and not session.fullscreen_notified:
                session.fullscreen_notified = True
                session.view.output.write(
                    Text(t("consoles.fullscreen.notice"), style="bold yellow")
                )
            if split == 0:
                # No transition in this chunk: render or discard under the
                # current state.
                if not session.fullscreen:
                    for line in self._feed(session, data):
                        session.view.output.write(line)
            else:
                before, after = data[:split], data[split:]
                # ``before`` belongs to the OLD state (rendered when not
                # fullscreen; silently dropped when fullscreen since it is
                # escape soup that the user already missed).
                if before and not was_fullscreen:
                    for line in self._feed(session, before):
                        session.view.output.write(line)
                # Trailing bytes after the marker belong to the NEW state:
                # discard while fullscreen, render otherwise.
                if after and not session.fullscreen:
                    for line in self._feed(session, after):
                        session.view.output.write(line)
        if session.proc.poll() is not None and not data:
            fd = session.proc.fd
            if fd is not None:
                asyncio.get_running_loop().remove_reader(fd)
            code = session.proc.poll()
            session.proc.close()
            # Flush the residual text waiting for a newline in ``session.buf``
            # so a partial last line (e.g. ``printf abc``) is not silently lost
            # when the process exits without a trailing newline.
            if session.buf:
                # ``AnsiDecoder.decode`` is a generator: materialise it before
                # handing the resulting Text segments to RichLog.
                for tail in session.decoder.decode(session.buf.rstrip("\r")):
                    session.view.output.write(tail)
                session.buf = ""
            session.view.output.write(Text(t("consoles.exited", code=code), style="dim"))

    @staticmethod
    def _scan_fullscreen(
        session: _Session, data: bytes,
    ) -> tuple[int, bool, bool]:
        """Track alternate-screen transitions in the raw pty byte stream.

        Returns ``(split_in_data, entered, exited)``:
        - ``split_in_data``: offset inside ``data`` where the first
          transition marker starts (0 when none in this chunk).
        - ``entered``: True iff the first marker was an enter marker.
        - ``exited``: True iff the first marker was an exit marker.

        The ``session.fullscreen`` flag is updated to reflect the new state,
        and the rolling ``scan_tail`` keeps the last ``_SCAN_TAIL`` bytes so
        a marker split across reads still matches.
        """
        window = session.scan_tail + data
        session.scan_tail = window[-_SCAN_TAIL:]
        # Find the FIRST transition (enter or exit) in the window so the
        # returned split point is the exact position of the marker that
        # flipped the state.
        candidates: list[tuple[int, str]] = []
        for marker in _ALT_ENTER:
            pos = window.find(marker)
            if pos != -1:
                candidates.append((pos, "enter"))
        for marker in _ALT_EXIT:
            pos = window.find(marker)
            if pos != -1:
                candidates.append((pos, "exit"))
        if not candidates:
            return 0, False, False
        candidates.sort()
        first_pos, kind = candidates[0]
        # Offset inside ``data`` (clip to 0 if the marker started in the tail).
        split_in_data = max(0, first_pos - (len(window) - len(data)))
        # Redundant markers (e.g. an ``enter`` while already fullscreen) are
        # no-ops: the resulting state is simply the marker kind.
        session.fullscreen = kind == "enter"
        return split_in_data, kind == "enter", kind == "exit"

    @staticmethod
    def _feed(session: _Session, data: bytes) -> list[Text]:
        """Decode raw pty bytes into styled lines (ANSI colors via rich)."""
        session.buf += data.decode("utf-8", errors="replace")
        raw_lines = session.buf.split("\n")
        session.buf = raw_lines.pop()  # incomplete tail waits for more data
        lines: list[Text] = []
        for raw in raw_lines:
            lines.extend(session.decoder.decode(raw.rstrip("\r")))
        return lines

    # ------------------------------------------------------------------ #
    # UI events
    # ------------------------------------------------------------------ #
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("con-tab-"):
            await self._activate(int(button_id.removeprefix("con-tab-")))
        elif button_id == "con-new":
            self._open_form(None)
        elif button_id == "con-edit":
            if self._active is not None:
                self._open_form(self._active)
        elif button_id == "con-delete":
            await self._delete_active()
        elif button_id == "con-interrupt":
            session = self._sessions.get(self._active) if self._active is not None else None
            if session is not None and session.proc.running:
                session.proc.interrupt()
        elif button_id == "con-terminal":
            if consoles.open_external_terminal(str(self._workdir)):
                self.notify(t("consoles.terminal.opened"))
            else:
                self.notify(t("consoles.terminal.unsupported"), severity="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "console-input":
            return
        session = self._sessions.get(self._active) if self._active is not None else None
        value = event.input.value
        event.input.value = ""
        if session is None or not session.proc.running:
            self.notify(t("consoles.warn.not_running"), severity="warning")
            return
        session.proc.write(value + "\n")

    def _open_form(self, index: int | None) -> None:
        spec = self._specs[index] if index is not None else None

        async def closed(result: ConsoleSpec | None) -> None:
            if result is None:
                return
            if index is None:
                self._specs.append(result)
                self._persist()
                await self._activate(len(self._specs) - 1)
            else:
                restart = result.command != self._specs[index].command
                self._specs[index] = result
                self._persist()
                if restart and index in self._sessions:
                    await self._teardown_session(index)  # command changed: fresh process
                await self._activate(index)

        self.app.push_screen(ConsoleFormScreen(spec), closed)

    async def _delete_active(self) -> None:
        if self._active is None:
            return
        index = self._active
        name = self._specs[index].name
        if index in self._sessions:
            await self._teardown_session(index)
        del self._specs[index]
        # Sessions are keyed by tab position: shift the keys after the gap.
        self._sessions = {
            (key - 1 if key > index else key): session
            for key, session in self._sessions.items()
        }
        self._active = None
        self._persist()
        self.notify(t("consoles.deleted", name=name))
        self._render_state()
        await self._refresh_tabs()
        if self._specs:
            await self._activate(min(index, len(self._specs) - 1))
