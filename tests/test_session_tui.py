"""Tests for the TUI behaviour under remote session mode."""

from __future__ import annotations

import asyncio
from pathlib import Path

from grafeno import models, remote, remotesession
from grafeno.app import GrafenoApp
from grafeno.tui.screens.tasks import NewTaskScreen, TaskListScreen
from grafeno.tui.widgets import LocationBar
from textual.widgets import DataTable, Input, Label


def _activate_fake_session(monkeypatch, tmp_path, *, remote_os: str = "Linux x86_64"):
    """Activate a fake session and return the home_mount Path."""
    monkeypatch.setattr(remote, "sshfs_available", lambda: True)
    home_mount = tmp_path / "session-home"
    home_mount.mkdir()
    remote.set_session(
        remote.RemoteSpec(user="root", host="h", path="/root"),
        mounts_base=tmp_path,
    )
    remotesession._current = remotesession.RemoteSession(
        spec=remote.RemoteSpec(user="root", host="h", path="/root"),
        remote_home="/root",
        remote_os=remote_os,
        home_mount=home_mount,
    )
    return home_mount


# ---------------------------------------------------------------------- #
# New-task form in session mode
# ---------------------------------------------------------------------- #
def test_new_task_form_session(monkeypatch, tmp_path):
    _activate_fake_session(monkeypatch, tmp_path)

    async def scenario():
        from grafeno import i18n

        i18n.set_language("en")
        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)
            # The remote workdir input is present, the manual remote field is NOT.
            assert app.screen.query_one("#nt-workdir", Input) is not None
            # In session mode the manual #nt-remote input is absent.
            matches = list(app.screen.query("#nt-remote"))
            assert matches == [], matches
            # The label uses the session-specific key (locator by rendered text).
            expected = i18n.t("nt.workdir.remote")
            labels = [
                str(child.render().plain if hasattr(child.render(), "plain") else child.render())
                for child in app.screen.query(Label)
            ]
            assert any(expected in text for text in labels), (expected, labels)

    asyncio.run(scenario())


def test_create_task_session(monkeypatch, tmp_path):
    _activate_fake_session(monkeypatch, tmp_path)
    gh_calls: list[int] = []
    from grafeno import gh as gh_module
    from grafeno.tui.screens import tasks as tasks_module

    monkeypatch.setattr(
        tasks_module.gh_module, "gh_available", lambda _wd: gh_calls.append(1) or True
    )

    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#nt-name", Input).value = "Session task"
            screen.query_one("#nt-workdir", Input).value = "/root/proyecto"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.workdir == "/root/proyecto"
            assert app.screen.current_task.remote == ""
            reloaded = models.load(app.screen.current_task.id)
            assert reloaded.workdir == "/root/proyecto"
            assert reloaded.remote == ""

    asyncio.run(scenario())


def test_create_task_session_bad_dir(monkeypatch, tmp_path):
    _activate_fake_session(monkeypatch, tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#nt-name", Input).value = "Mala ruta"
            screen.query_one("#nt-workdir", Input).value = "relative/path"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            for _ in range(5):
                await pilot.pause(0.05)
            # The modal stays open, no task was created.
            assert isinstance(app.screen, NewTaskScreen)
            assert not models.list_all()

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# LocationBar in session mode
# ---------------------------------------------------------------------- #
def test_location_bar_session(monkeypatch, tmp_path):
    _activate_fake_session(monkeypatch, tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.screen.query_one("#location-bar", LocationBar)
            bar._render_bar()
            await pilot.pause()
            rendered = bar.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "root@h" in text

    asyncio.run(scenario())


def test_location_bar_task_badge(monkeypatch, tmp_path):
    _activate_fake_session(monkeypatch, tmp_path)

    async def scenario():
        task = type("T", (), {"is_remote": False, "remote": "", "workdir": "/x"})()
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            bar = app.screen.query_one("#location-bar", LocationBar)
            bar.set_task(task)  # type: ignore[arg-type]
            await pilot.pause()
            rendered = bar.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            # The session gives a [SSH] badge even for tasks without task.remote.
            assert "[SSH]" in text
            assert "root@h" in text

    asyncio.run(scenario())
