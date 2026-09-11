"""Tests of the processing-profiles editor widget."""

from __future__ import annotations

import asyncio

from grafeno.config import RoleConfig
from grafeno.profiles import Profile
from grafeno.tui.profilesform import ProfilesForm


def _holder_app():
    """Build a tiny App that hosts a single ``ProfilesForm``."""
    from textual.app import App

    class Holder(App):
        def compose(self):
            yield ProfilesForm()

    return Holder()


def _sample(name="calidad") -> Profile:
    profile = Profile(name=name)
    profile.planner = RoleConfig(cli="opencode", model="m1")
    profile.implementer = RoleConfig(cli="kimi", model="k3")
    return profile


def test_set_and_get_profiles_roundtrip():
    """``set_profiles`` stores and ``profiles()`` returns a copy."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ProfilesForm)
            form.set_profiles([_sample(), Profile(name="rapido")])
            out = form.profiles()
            assert [p.name for p in out] == ["calidad", "rapido"]
            assert out is not form._profiles  # it's a copy

    asyncio.run(scenario())


def test_set_profiles_replaces_existing_list():
    """A second ``set_profiles`` replaces the first list entirely."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ProfilesForm)
            form.set_profiles([_sample("a")])
            form.set_profiles([_sample("b"), _sample("c")])
            assert [p.name for p in form.profiles()] == ["b", "c"]

    asyncio.run(scenario())


def test_profiles_form_mounts_table_and_buttons():
    """The compose tree matches the documented widget IDs."""

    async def scenario():
        from textual.widgets import Button, DataTable

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prof-table", DataTable)
            app.query_one("#prof-add", Button)
            app.query_one("#prof-edit", Button)
            app.query_one("#prof-delete", Button)

    asyncio.run(scenario())


def test_apply_edit_inserts_and_replaces():
    """``_apply_edit`` adds new rows and replaces existing ones by index."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ProfilesForm)
            form.set_profiles([_sample("first")])
            form._apply_edit(-1, _sample("inserted"))
            assert [p.name for p in form.profiles()] == ["first", "inserted"]
            replacement = _sample("replaced")
            form._apply_edit(0, replacement)
            assert [p.name for p in form.profiles()] == ["replaced", "inserted"]

    asyncio.run(scenario())


def test_delete_removes_first_row_and_keeps_order():
    """Selecting the first row and pressing delete keeps the remaining items."""

    async def scenario():
        from textual.widgets import DataTable

        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ProfilesForm)
            form.set_profiles([_sample("a"), _sample("b"), _sample("c")])
            await pilot.pause()
            table = app.query_one(DataTable)
            assert table.row_count == 3
            await pilot.click("#prof-delete")
            await pilot.pause()
            assert [p.name for p in form.profiles()] == ["b", "c"]

    asyncio.run(scenario())


def test_delete_on_empty_table_is_noop():
    """Pressing delete on an empty table does not raise."""

    async def scenario():
        app = _holder_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            form = app.query_one(ProfilesForm)
            assert form.profiles() == []
            await pilot.click("#prof-delete")
            await pilot.pause()
            assert form.profiles() == []

    asyncio.run(scenario())