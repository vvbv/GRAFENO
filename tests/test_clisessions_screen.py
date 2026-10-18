"""Smoke tests of the direct CLI sessions screen of the TUI."""

from __future__ import annotations

import asyncio

from textual.widgets import Button, DataTable, Input, RichLog

from grafeno import clisessions, config as config_module
from grafeno.app import GrafenoApp
from grafeno.clisessions import SESSION_CLIS
from grafeno.drivers.base import CLIDriver, EventKind, RunEvent, RunResult, TokenUsage
from grafeno.tui.screens.clisessions import CliSessionsScreen, NewCliSessionScreen
from grafeno.tui.screens.projects import ProjectActionsScreen, ProjectsScreen
from grafeno.tui.screens.tasks import TaskListScreen


class FakeSessionDriver(CLIDriver):
    name = "claude"
    display_name = "Fake Claude"
    executable = "fake"

    def is_available(self) -> bool:
        return True

    async def run(self, request, on_event=None, on_activity=None, on_session=None):
        if on_session:
            on_session("native-1")
        if on_event:
            on_event(RunEvent(EventKind.TEXT, f"echo: {request.prompt}"))
        return RunResult(ok=True, session_id="native-1", tokens=TokenUsage(1, 1))


async def _fake_models(clis):
    return {cli: [] for cli in clis}


async def _fake_variants(clis):
    return {}


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ws = (tmp_path / "ws").resolve()
    (ws / "alpha").mkdir(parents=True)
    cfg = config_module.load()
    cfg.workspaces = [str(ws)]
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.clisessions.fetch_all_models", _fake_models)
    monkeypatch.setattr("grafeno.tui.screens.clisessions.fetch_all_variants", _fake_variants)
    return ws / "alpha"


async def _wait_for(pilot, app, screen_type) -> None:
    for _ in range(40):
        await pilot.pause(0.05)
        if isinstance(app.screen, screen_type):
            await pilot.pause()
            return


def test_open_sessions_from_project_actions_and_create(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)

    async def scenario():
        app = GrafenoApp()
        monkeypatch.setattr(app.cli_sessions, "_driver_factory", lambda cli: FakeSessionDriver())
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _wait_for(pilot, app, ProjectsScreen)
            await pilot.press("enter")
            await _wait_for(pilot, app, ProjectActionsScreen)
            await pilot.click("#pa-sessions")
            await _wait_for(pilot, app, CliSessionsScreen)
            assert isinstance(app.screen, CliSessionsScreen)
            assert app.screen._workdir == str(project)
            await pilot.click("#cs-table")
            await pilot.press("n")
            await _wait_for(pilot, app, NewCliSessionScreen)
            assert isinstance(app.screen, NewCliSessionScreen)
            await pilot.click("#cs-new-ok")
            await _wait_for(pilot, app, CliSessionsScreen)
            assert app.screen.query_one("#cs-table", DataTable).row_count == 1
            sessions = clisessions.list_all(str(project))
            assert len(sessions) == 1 and sessions[0].cli in SESSION_CLIS

            await pilot.click("#cs-input")
            await pilot.press(*"hola")
            await pilot.press("enter")
            for _ in range(40):
                await pilot.pause(0.05)
                entries, _ = clisessions.load_transcript(sessions[0].id)
                if any(e["kind"] == "text" for e in entries):
                    break
            kinds = {e["kind"] for e in clisessions.load_transcript(sessions[0].id)[0]}
            assert {"user", "text"} <= kinds
            await pilot.pause(0.2)
            log_text = "\n".join(line.text for line in app.screen.query_one("#cs-log", RichLog).lines)
            assert "> hola" in log_text
            assert "No messages yet" not in log_text

            await pilot.click("#cs-toggle")
            await pilot.pause()
            assert app.screen.query_one("#cs-input", Input).disabled
            # Direct press: a second pilot click right after the first one
            # is delivered as a double click and does not reach the button.
            app.screen.query_one("#cs-toggle", Button).press()
            await pilot.pause(0.2)
            assert not app.screen.query_one("#cs-input", Input).disabled
            assert str(app.screen.query_one("#cs-toggle", Button).label) == "Close"

    asyncio.run(scenario())


def test_sessions_key_from_task_list_opens_all_sessions(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            await pilot.press("s")
            await _wait_for(pilot, app, CliSessionsScreen)
            assert isinstance(app.screen, CliSessionsScreen)
            assert app.screen._workdir is None

    asyncio.run(scenario())


def test_empty_notice_is_not_duplicated_and_all_view_text(tmp_path, monkeypatch):
    project = _setup(tmp_path, monkeypatch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await _wait_for(pilot, app, CliSessionsScreen)
            assert "Open a project" in str(app.screen.query_one("#cs-head").render())
            session = app.cli_sessions.create(str(project), "claude")
            await pilot.pause(0.2)
            for _ in range(3):
                app.cli_sessions.rename(session.id, "x")
                app.cli_sessions.close(session.id)
                app.cli_sessions.reopen(session.id)
            await pilot.pause(0.2)
            text = "\n".join(line.text for line in app.screen.query_one("#cs-log", RichLog).lines)
            assert text.count("No messages yet") == 1

    asyncio.run(scenario())
