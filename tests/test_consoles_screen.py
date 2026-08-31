"""Tests of the consoles screen (Textual headless, fake pty process)."""

from __future__ import annotations

import asyncio
import os

from grafeno import consoles
from grafeno.app import GrafenoApp
from grafeno.consoles import ConsoleSpec
from grafeno.tui.screens import consoles as consoles_module
from grafeno.tui.screens.consoles import ConsoleFormScreen, ConsolesScreen


class FakeConsole:
    """In-memory ConsoleProcess stand-in built on a real pipe.

    ``push`` writes to the pipe; ``kill`` simulates the child process exiting
    (closes the write end so the read side sees EOF without removing the
    reader fd), and ``close`` is the full teardown (closes both ends).
    """

    instances: list["FakeConsole"] = []

    def __init__(self, command: str, workdir: str):
        self.command = command
        self.workdir = workdir
        self.written: list[str] = []
        self._r: int | None = None
        self._w: int | None = None
        self._alive = False
        FakeConsole.instances.append(self)

    @property
    def fd(self):
        return self._r

    @property
    def running(self):
        return self._alive

    def start(self):
        self._r, self._w = os.pipe()
        os.set_blocking(self._r, False)
        self._alive = True

    def read(self, size: int = 65536):
        if self._r is None:
            return b""
        try:
            return os.read(self._r, size)
        except BlockingIOError:
            return b""

    def write(self, data: str):
        self.written.append(data)

    def interrupt(self):
        self.written.append("\x03")

    def poll(self):
        return None if self._alive else 0

    def push(self, data: bytes):
        os.write(self._w, data)

    def kill(self):
        """Simulate the child process exiting: close the write end so the read
        side sees EOF, but keep the read side open so add_reader still fires.
        Mirrors the real PTY behaviour where the slave side closes first."""
        self._alive = False
        if self._w is not None:
            try:
                os.close(self._w)
            except OSError:
                pass
        self._w = None

    def close(self):
        """Full teardown: close both ends of the pipe."""
        self._alive = False
        for fd in (self._r, self._w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._r = self._w = None


def _install_fake(monkeypatch):
    FakeConsole.instances = []
    monkeypatch.setattr(consoles_module, "ConsoleProcess", FakeConsole)


def _log_text(screen) -> str:
    from textual.widgets import RichLog
    if not screen.query("#con-view-0"):
        return ""
    log = screen.query_one("#con-view-0 RichLog")
    # RichLog.lines is an INSTANCE attribute (list of Strip; Strip.text works).
    return "".join(strip.text for strip in log.lines)


def test_consoles_screen_empty_state(monkeypatch, tmp_path):
    _install_fake(monkeypatch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            from textual.widgets import Static
            hint = app.screen.query_one("#console-empty", Static)
            assert hint.display is True
            assert not app.screen.query("#con-tab-0")
            assert FakeConsole.instances == []

    asyncio.run(scenario())


def test_new_console_creates_tab_spawns_and_persists(monkeypatch, tmp_path):
    _install_fake(monkeypatch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            await pilot.click("#con-new")
            await pilot.pause()
            assert isinstance(app.screen, ConsoleFormScreen)
            from textual.widgets import Input
            app.screen.query_one("#cf-name", Input).value = "servidor"
            await pilot.click("#cf-save")
            await pilot.pause()
            assert isinstance(app.screen, ConsolesScreen)
            assert app.screen.query("#con-tab-0")
            assert len(FakeConsole.instances) == 1  # spawned on activation
            assert consoles.load_project(tmp_path) == [ConsoleSpec(name="servidor")]

    asyncio.run(scenario())


def test_console_output_renders_and_input_reaches_process(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            fake = FakeConsole.instances[0]
            fake.push(b"hola \x1b[31mrojo\x1b[0m\n")
            for _ in range(10):
                await pilot.pause(0.05)
                if "hola" in _log_text(app.screen):
                    break
            assert "hola rojo" in _log_text(app.screen)

            from textual.widgets import Input
            entry = app.screen.query_one("#console-input", Input)
            entry.value = "ls -la"
            await entry.action_submit()
            await pilot.pause()
            assert "ls -la\n" in fake.written

    asyncio.run(scenario())


def test_edit_console_renames_and_recolors(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="viejo")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            await pilot.click("#con-edit")
            await pilot.pause()
            from textual.widgets import Input, Select
            app.screen.query_one("#cf-name", Input).value = "nuevo"
            app.screen.query_one("#cf-color", Select).value = "red"
            await pilot.click("#cf-save")
            await pilot.pause()
            specs = consoles.load_project(tmp_path)
            assert specs == [ConsoleSpec(name="nuevo", color="red")]
            tab = app.screen.query_one("#con-tab-0")
            assert str(tab.label) == "nuevo"
            background = tab.styles.background
            # Textual resolves named colors to RGB values (Color(255, 0, 0) for
            # ``red``); accept either the name or the equivalent RGB triple.
            color_name = getattr(background, "name", None)
            assert color_name == "red" or str(background) == "Color(255, 0, 0)"

    asyncio.run(scenario())


def test_delete_console_removes_tab_and_persists(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="uno"), ConsoleSpec(name="dos")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            assert app.screen.query("#con-tab-0") and app.screen.query("#con-tab-1")
            await pilot.click("#con-delete")  # deletes the active tab ("uno")
            await pilot.pause()
            assert consoles.load_project(tmp_path) == [ConsoleSpec(name="dos")]
            tabs = app.screen.query(".console-tab")
            assert len(tabs) == 1
            assert str(tabs[0].label) == "dos"

    asyncio.run(scenario())


def test_unsupported_platform_shows_notice(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    monkeypatch.setattr(consoles, "supported", lambda: False)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            from textual.widgets import Static
            hint = app.screen.query_one("#console-empty", Static)
            assert hint.display is True
            assert "POSIX" in str(hint.render())
            assert FakeConsole.instances == []  # nothing spawned

    asyncio.run(scenario())


def test_dead_process_respawns_on_tab_click(monkeypatch, tmp_path):
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            first = FakeConsole.instances[0]
            first.kill()  # simulate the shell exiting (write end closed)
            await pilot.click("#con-tab-0")
            await pilot.pause()
            assert len(FakeConsole.instances) == 2
            assert FakeConsole.instances[1].running

    asyncio.run(scenario())


def test_task_list_opens_consoles_via_button_and_binding(monkeypatch, tmp_path):
    """The task list has a Consoles button and the 'k' binding."""
    _install_fake(monkeypatch)
    monkeypatch.chdir(tmp_path)  # consoles persist into <cwd>/.grafeno.toml

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from grafeno.tui.screens.tasks import TaskListScreen
            assert isinstance(app.screen, TaskListScreen)
            keys = {b.key for b in TaskListScreen.BINDINGS if isinstance(b.key, str)}
            assert "k" in keys
            await pilot.click("#consoles-open")
            await pilot.pause()
            assert isinstance(app.screen, ConsolesScreen)
            assert app.screen._workdir == tmp_path
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_task_detail_opens_consoles_with_task_workdir(monkeypatch, tmp_path):
    """The task detail opens the consoles of the task's project via 'k'."""
    _install_fake(monkeypatch)
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task

    project = tmp_path / "proyecto"
    project.mkdir()
    task = Task.create("Con consolas", "desc", str(project), Config())
    models.save(task)

    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            keys = {b.key for b in TaskDetailScreen.BINDINGS if isinstance(b.key, str)}
            assert "k" in keys
            await pilot.press("k")
            await pilot.pause()
            assert isinstance(app.screen, ConsolesScreen)
            assert app.screen._workdir == project

    asyncio.run(scenario())


def test_create_after_delete_middle_does_not_duplicate_ids(monkeypatch, tmp_path):
    """Regression: opening all 3 tabs, deleting the middle one and creating a
    new console used to raise DuplicateIds because ConsoleView ids were tied
    to the tab position. Each ConsoleView must keep a unique monotonic id.
    """
    _install_fake(monkeypatch)
    consoles.save_project(
        tmp_path,
        [ConsoleSpec(name="A"), ConsoleSpec(name="B"), ConsoleSpec(name="C")],
    )

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            # Activate every tab so every spec gets a ConsoleView mounted.
            await pilot.click("#con-tab-0")
            await pilot.pause()
            await pilot.click("#con-tab-1")
            await pilot.pause()
            await pilot.click("#con-tab-2")
            await pilot.pause()
            # Switch back to the middle tab and delete it (so the trailing
            # tab keeps the same index, exercising the key-shift path).
            await pilot.click("#con-tab-1")
            await pilot.pause()
            await pilot.click("#con-delete")
            await pilot.pause()
            assert consoles.load_project(tmp_path) == [
                ConsoleSpec(name="A"),
                ConsoleSpec(name="C"),
            ]
            # Now create a new console: this used to crash with DuplicateIds.
            await pilot.click("#con-new")
            await pilot.pause()
            from textual.widgets import Input
            app.screen.query_one("#cf-name", Input).value = "D"
            await pilot.click("#cf-save")
            await pilot.pause()
            assert consoles.load_project(tmp_path) == [
                ConsoleSpec(name="A"),
                ConsoleSpec(name="C"),
                ConsoleSpec(name="D"),
            ]
            # Every ConsoleView mounted on the screen has a unique id.
            views = list(app.screen.query_one("#console-views").children)
            ids = [view.id for view in views]
            assert len(ids) == len(set(ids)), f"duplicate view ids: {ids}"

    asyncio.run(scenario())


def test_dead_process_flushes_residual_output_without_newline(monkeypatch, tmp_path):
    """Regression: a partial last line (no trailing newline) used to be lost
    when the process exited. It must be flushed to the transcript before the
    exit announcement is written.
    """
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            fake = FakeConsole.instances[0]
            # Push a complete line and a partial one (no trailing newline),
            # then kill the process to trigger the exit branch.
            fake.push(b"primero\nsegundo")
            for _ in range(10):
                await pilot.pause(0.05)
                if "primero" in _log_text(app.screen):
                    break
            fake.kill()  # closes write end: read side will see EOF
            for _ in range(10):
                await pilot.pause(0.05)
                text = _log_text(app.screen)
                if "segundo" in text:
                    break
            text = _log_text(app.screen)
            assert "primero" in text
            assert "segundo" in text  # flushed even without a final newline

    asyncio.run(scenario())


def test_console_buttons_are_compact(monkeypatch, tmp_path):
    """All console buttons render in compact mode (one line tall)."""
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        from textual.widgets import Button
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Entry point in the task list header.
            assert app.screen.query_one("#consoles-open", Button).compact is True
            assert app.screen.query_one("#scope-toggle", Button).compact is True
            # Consoles screen: action buttons and tabs.
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            for button_id in ("#con-new", "#con-edit", "#con-delete", "#con-interrupt", "#con-terminal"):
                assert app.screen.query_one(button_id, Button).compact is True
            tab = app.screen.query_one("#con-tab-0", Button)
            assert tab.compact is True
            assert tab.outer_size.height == 1

    asyncio.run(scenario())


def test_fullscreen_program_shows_notice_and_suppresses_output(monkeypatch, tmp_path):
    """Alt-screen output triggers a one-time notice and its escape soup is hidden."""
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            fake = FakeConsole.instances[0]
            fake.push(b"antes\n\x1b[?1049h")          # enters full-screen mode
            fake.push(b"\x1b[2J\x1b[1;1Htui-basura")  # swallowed while full-screen
            for _ in range(10):
                await pilot.pause(0.05)
                if "full-screen" in _log_text(app.screen):
                    break
            text = _log_text(app.screen)
            assert "antes" in text
            assert "full-screen" in text
            assert "tui-basura" not in text
            fake.push(b"\x1b[?1049hmas-basura")        # second enter: no new notice
            for _ in range(5):
                await pilot.pause(0.05)
            assert _log_text(app.screen).count("full-screen program detected") == 1
            fake.push(b"\x1b[?1049l" + b"despues\n")   # leaves full-screen mode
            for _ in range(10):
                await pilot.pause(0.05)
                if "despues" in _log_text(app.screen):
                    break
            text = _log_text(app.screen)
            assert "despues" in text
            assert "mas-basura" not in text

    asyncio.run(scenario())


def test_terminal_button_opens_external_terminal(monkeypatch, tmp_path):
    """The Terminal button delegates to consoles.open_external_terminal."""
    _install_fake(monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr(
        consoles,
        "open_external_terminal",
        lambda workdir: opened.append(workdir) or True,
    )
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            await pilot.click("#con-terminal")
            await pilot.pause()
            assert opened == [str(tmp_path)]

    asyncio.run(scenario())


def test_fullscreen_notice_when_marker_split_across_reads(monkeypatch, tmp_path):
    """Regression: an alt-screen enter marker arriving in two reads still
    shows the notice (the marker lives entirely in the rolling scan_tail
    after the second read, so ``split`` is 0 but the state flipped).
    """
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            fake = FakeConsole.instances[0]
            # Split \\x1b[?1049h into two reads. The first read has no
            # complete marker, so the scan_tail carries the partial bytes
            # into the second read where the marker finally matches.
            fake.push(b"hola\n\x1b[?10")
            for _ in range(10):
                await pilot.pause(0.05)
                if "hola" in _log_text(app.screen):
                    break
            assert "hola" in _log_text(app.screen)
            assert "full-screen" not in _log_text(app.screen)  # not yet
            fake.push(b"49h")
            for _ in range(10):
                await pilot.pause(0.05)
                if "full-screen" in _log_text(app.screen):
                    break
            text = _log_text(app.screen)
            assert "full-screen" in text  # notice fires despite split==0

    asyncio.run(scenario())


def test_submitted_command_is_locally_echoed(monkeypatch, tmp_path):
    """The submitted command is rendered next to the pending prompt, and not
    echoed at all while a full-screen program is active."""
    _install_fake(monkeypatch)
    consoles.save_project(tmp_path, [ConsoleSpec(name="shell")])

    async def scenario():
        from textual.widgets import Input
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(ConsolesScreen(tmp_path))
            await pilot.pause()
            fake = FakeConsole.instances[0]
            fake.push(b"daniel@host$ ")  # prompt without trailing newline
            for _ in range(5):
                await pilot.pause(0.05)
            entry = app.screen.query_one("#console-input", Input)
            entry.value = "ls -la"
            await entry.action_submit()
            await pilot.pause()
            assert "daniel@host$ ls -la" in _log_text(app.screen)  # one line
            assert "ls -la\n" in fake.written

            # Full-screen program: no local echo (input goes to the TUI).
            fake.push(b"\x1b[?1049h")
            for _ in range(5):
                await pilot.pause(0.05)
            entry = app.screen.query_one("#console-input", Input)
            entry.value = "dentro-del-tui"
            await entry.action_submit()
            await pilot.pause()
            assert "dentro-del-tui" not in _log_text(app.screen)
            assert "dentro-del-tui\n" in fake.written

    asyncio.run(scenario())
