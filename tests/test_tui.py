"""Smoke tests de la TUI (Textual headless)."""

from __future__ import annotations

import asyncio

from grafeno.app import GrafenoApp
from grafeno.tui.screens.tasks import NewTaskScreen, TaskListScreen
from textual.widgets import DataTable, Input


def test_app_boots_into_task_list():
    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert app.screen.query_one(DataTable).row_count == 0

    asyncio.run(scenario())


def test_create_task_via_modal():
    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)

            app.screen.query_one("#nt-name", Input).value = "Tarea de prueba"
            await pilot.click("#nt-create")
            await pilot.pause()

            # Tras crear, se abre el detalle de la tarea.
            assert isinstance(app.screen, TaskDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert app.screen.query_one(DataTable).row_count == 1

    asyncio.run(scenario())


def test_open_task_detail():
    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#nt-name", Input).value = "Detalle"
            await pilot.click("#nt-create")
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            # Volvemos a la lista.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())
