"""Smoke tests of the projects screen (working directories) of the TUI."""

from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, Static

from grafeno import config as config_module
from grafeno import models
from grafeno.app import GrafenoApp
from grafeno.config import Config
from grafeno.tui.screens.projects import ProjectActionsScreen, ProjectsScreen
from grafeno.tui.screens.tasks import NewTaskScreen, TaskListScreen


def _workspace(tmp_path):
    """Working directory with two projects (one with a task) saved in the config."""
    ws = (tmp_path / "ws").resolve()
    (ws / "alpha").mkdir(parents=True)
    (ws / "Beta").mkdir()
    (ws / ".hidden").mkdir()
    (ws / "notes.txt").write_text("x", encoding="utf-8")
    cfg = config_module.load()
    cfg.workspaces = [str(ws)]
    config_module.save(cfg)
    models.save(models.Task.create("A", "d", str(ws / "alpha"), Config()))
    return ws


def _rows(table: DataTable) -> list[list[str]]:
    return [[str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)]


async def _wait_for(pilot, app, screen_type) -> None:
    for _ in range(40):
        await pilot.pause(0.05)
        if isinstance(app.screen, screen_type):
            await pilot.pause()
            return


def test_projects_screen_lists_and_filters_folders(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _wait_for(pilot, app, ProjectsScreen)
            assert isinstance(app.screen, ProjectsScreen)
            table = app.screen.query_one("#projects-table", DataTable)
            assert _rows(table) == [
                ["alpha", "1", str(ws / "alpha")],
                ["Beta", "0", str(ws / "Beta")],
            ]
            app.screen.query_one("#projects-filter", Input).value = "bet"
            await pilot.pause()
            assert [row[0] for row in _rows(table)] == ["Beta"]
            hint = str(app.screen.query_one("#projects-hint", Static).render())
            assert "1 of 2 projects" in hint

    asyncio.run(scenario())


def test_project_actions_open_new_task_with_the_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _wait_for(pilot, app, ProjectsScreen)
            await pilot.press("down")   # cursor on "Beta"
            await pilot.press("enter")  # row selected: actions dialog
            await _wait_for(pilot, app, ProjectActionsScreen)
            assert isinstance(app.screen, ProjectActionsScreen)
            await pilot.click("#pa-new-task")
            await _wait_for(pilot, app, NewTaskScreen)
            assert isinstance(app.screen, NewTaskScreen)
            assert app.screen.query_one("#nt-workdir", Input).value == str(ws / "Beta")

    asyncio.run(scenario())


def test_new_task_shortcut_uses_the_highlighted_project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = _workspace(tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _wait_for(pilot, app, ProjectsScreen)
            await pilot.press("n")
            await _wait_for(pilot, app, NewTaskScreen)
            assert isinstance(app.screen, NewTaskScreen)
            assert app.screen.query_one("#nt-workdir", Input).value == str(ws / "alpha")

    asyncio.run(scenario())


def test_projects_screen_without_workspaces_shows_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _wait_for(pilot, app, ProjectsScreen)
            assert app.screen.query_one("#projects-table", DataTable).row_count == 0
            hint = str(app.screen.query_one("#projects-hint", Static).render())
            assert "No working directories configured" in hint
            await pilot.press("escape")
            await _wait_for(pilot, app, TaskListScreen)
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_projects_screen_in_session_mode(tmp_path, monkeypatch):
    from grafeno import remote, remotesession

    monkeypatch.chdir(tmp_path)
    _workspace(tmp_path)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            remotesession._current = remotesession.RemoteSession(
                spec=remote.RemoteSpec(user="root", host="h", path="/root"), remote_home="/root",
            )
            app.push_screen(ProjectsScreen())
            await _wait_for(pilot, app, ProjectsScreen)
            assert app.screen.query_one("#projects-table", DataTable).row_count == 0
            hint = str(app.screen.query_one("#projects-hint", Static).render())
            assert "remote session" in hint

    asyncio.run(scenario())
