"""Tests of the reusable references form widget."""

from __future__ import annotations

import asyncio

from grafeno.references import Reference
from grafeno.tui.refform import ReferencesForm


def _holder_app():
    """Build a tiny App that hosts a single ``ReferencesForm``."""
    from textual.app import App

    class Holder(App):
        def compose(self):
            yield ReferencesForm()

    return Holder()


def test_set_and_get_references_roundtrip():
    """``set_references`` stores and ``references`` returns a copy."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            refs = [
                Reference(name="r1", description="d1", path="/x"),
                Reference(name="r2", path="https://e"),
            ]
            form.set_references(refs)
            out = form.references()
            assert out == refs
            assert out is not refs  # it's a copy

    asyncio.run(scenario())


def test_set_references_replaces_existing_list():
    """A second ``set_references`` replaces the first list entirely."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            form.set_references([Reference(name="a", path="/a")])
            form.set_references(
                [Reference(name="b", path="/b"), Reference(name="c", path="/c")]
            )
            assert [ref.name for ref in form.references()] == ["b", "c"]

    asyncio.run(scenario())


def test_references_form_mounts_table_and_inputs():
    """The compose tree matches the documented widget IDs."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Button, DataTable, Input

            app.query_one("#refs-table", DataTable)
            app.query_one("#refs-name", Input)
            app.query_one("#refs-description", Input)
            app.query_one("#refs-path", Input)
            app.query_one("#refs-add", Button)
            app.query_one("#refs-delete", Button)

    asyncio.run(scenario())


def test_add_appends_via_inputs():
    """Filling the inputs and adding appends to the list and clears them."""

    async def scenario():
        from textual.widgets import Input

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            form.query_one("#refs-name", Input).value = "name"
            form.query_one("#refs-description", Input).value = "desc"
            form.query_one("#refs-path", Input).value = "/x"
            await pilot.pause()
            await pilot.click("#refs-add")
            await pilot.pause()
            assert [ref.name for ref in form.references()] == ["name"]
            assert form.query_one("#refs-name", Input).value == ""
            assert form.query_one("#refs-description", Input).value == ""
            assert form.query_one("#refs-path", Input).value == ""

    asyncio.run(scenario())


def test_add_without_name_does_not_append():
    """An add without name shows an error and the list stays empty."""

    async def scenario():
        from textual.widgets import Input

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            form.query_one("#refs-path", Input).value = "/x"
            await pilot.pause()
            await pilot.click("#refs-add")
            await pilot.pause()
            assert form.references() == []

    asyncio.run(scenario())


def test_add_without_path_does_not_append():
    """An add without path shows an error and the list stays empty."""

    async def scenario():
        from textual.widgets import Input

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            form.query_one("#refs-name", Input).value = "only-name"
            await pilot.pause()
            await pilot.click("#refs-add")
            await pilot.pause()
            assert form.references() == []

    asyncio.run(scenario())


def test_delete_removes_first_row_and_keeps_order():
    """Selecting the first row and pressing delete keeps the remaining items."""

    async def scenario():
        from textual.widgets import DataTable

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ReferencesForm)
            form.set_references([
                Reference(name="a", path="/a"),
                Reference(name="b", path="/b"),
                Reference(name="c", path="/c"),
            ])
            await pilot.pause()
            table = app.query_one(DataTable)
            assert table.row_count == 3
            await pilot.click("#refs-delete")
            await pilot.pause()
            assert [ref.name for ref in form.references()] == ["b", "c"]

    asyncio.run(scenario())

