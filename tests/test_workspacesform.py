"""Tests of the reusable working-directories form widget."""

from __future__ import annotations

import asyncio

from textual.widgets import Button, DataTable, Input

from grafeno.tui.workspacesform import WorkspacesForm


def _holder_app():
    """Build a tiny App that hosts a single ``WorkspacesForm``."""
    from textual.app import App

    class Holder(App):
        def compose(self):
            yield WorkspacesForm()

    return Holder()


def _cells(table: DataTable, column: int) -> list[str]:
    return [str(table.get_row_at(index)[column]) for index in range(table.row_count)]


def test_form_mounts_documented_widgets():
    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#ws-table", DataTable)
            app.query_one("#ws-path", Input)
            app.query_one("#ws-add", Button)
            app.query_one("#ws-delete", Button)

    asyncio.run(scenario())


def test_set_workspaces_normalizes_and_reports_state(tmp_path):
    ws = tmp_path / "ws"
    (ws / "one").mkdir(parents=True)
    (ws / "two").mkdir()

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(WorkspacesForm)
            form.set_workspaces([str(ws) + "/", str(ws), "", str(tmp_path / "missing")])
            assert form.workspaces() == [str(ws), str(tmp_path / "missing")]
            table = app.query_one("#ws-table", DataTable)
            assert _cells(table, 0) == [str(ws), str(tmp_path / "missing")]
            assert _cells(table, 1) == ["2 project(s)", "not found"]

    asyncio.run(scenario())


def test_add_rejects_duplicates_and_delete_removes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    async def scenario():
        app = _holder_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            form = app.query_one(WorkspacesForm)
            path_input = app.query_one("#ws-path", Input)
            path_input.value = str(ws) + "/"
            await pilot.pause()
            await pilot.click("#ws-add")
            await pilot.pause()
            assert form.workspaces() == [str(ws)]
            assert path_input.value == ""
            path_input.value = str(ws)
            form._add()  # duplicate: rejected, the text stays in the input
            assert path_input.value == str(ws)
            path_input.value = ""
            await pilot.click("#ws-delete")
            await pilot.pause()
            assert form.workspaces() == []

    asyncio.run(scenario())


def test_pending_input_is_kept_when_reading():
    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(WorkspacesForm)
            form.set_workspaces(["/srv/a"])
            app.query_one("#ws-path", Input).value = "/srv/b/"
            assert form.workspaces() == ["/srv/a", "/srv/b"]

    asyncio.run(scenario())
