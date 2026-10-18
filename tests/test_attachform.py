"""Tests of the reusable attachments editor."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input

from grafeno import media
from grafeno.tui.attachform import AttachmentsForm


class _Host(App):
    def compose(self) -> ComposeResult:
        yield AttachmentsForm(id="form")


def _with_form(scenario):
    async def run():
        app = _Host()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await scenario(app, app.query_one("#form", AttachmentsForm), pilot)

    asyncio.run(run())


def test_missing_file_is_not_added(tmp_path):
    async def scenario(app, form, pilot):
        form.query_one("#att-path", Input).value = str(tmp_path / "nope.docx")
        form._add()
        assert form.paths() == []

    _with_form(scenario)


def test_duplicate_is_not_added(tmp_path):
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"x")

    async def scenario(app, form, pilot):
        for _ in range(2):
            form.query_one("#att-path", Input).value = str(source)
            form._add()
        assert form.paths() == [source]

    _with_form(scenario)


def test_save_into_copies_files(tmp_path):
    source = tmp_path / "a.xlsx"
    source.write_bytes(b"XLSX")

    async def scenario(app, form, pilot):
        form.query_one("#att-path", Input).value = str(source)
        form._add()
        assert form.save_into("t-attform", origin="tui", cycle=1) == (1, [])
        assert media.list_attachments("t-attform")[0].name == "a.xlsx"

    _with_form(scenario)


def test_save_into_does_not_read_whole_file(tmp_path, monkeypatch):
    import pathlib

    source = tmp_path / "big.zip"
    source.write_bytes(b"ZIP")

    def boom(self):
        raise AssertionError("read_bytes must not be used")

    async def scenario(app, form, pilot):
        form.query_one("#att-path", Input).value = str(source)
        form._add()
        with monkeypatch.context() as patch:
            patch.setattr(pathlib.Path, "read_bytes", boom)
            assert form.save_into("t-attform-big", origin="tui", cycle=1) == (1, [])
        assert media.list_attachments("t-attform-big")[0].name == "big.zip"

    _with_form(scenario)
