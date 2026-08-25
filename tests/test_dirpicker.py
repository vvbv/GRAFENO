"""Tests of the directory selector (pure function + TUI smoke)."""

from __future__ import annotations

import asyncio
import os

from grafeno.tui.dirpicker import directory_matches


def test_directory_matches_completes_last_segment(tmp_path):
    (tmp_path / "proyectos").mkdir()
    (tmp_path / "publico").mkdir()
    (tmp_path / "archivo.txt").write_text("x")  # files do not appear

    base = str(tmp_path) + os.sep
    matches = directory_matches(base + "p")
    assert str(tmp_path / "proyectos") + os.sep in matches
    assert str(tmp_path / "publico") + os.sep in matches
    assert all(m.endswith(os.sep) for m in matches)
    assert not any("archivo" in m for m in matches)


def test_directory_matches_trailing_separator_lists_children(tmp_path):
    (tmp_path / "hijo").mkdir()
    matches = directory_matches(str(tmp_path) + os.sep)
    assert matches == [str(tmp_path / "hijo") + os.sep]


def test_directory_matches_hidden_and_missing(tmp_path):
    (tmp_path / ".oculto").mkdir()
    (tmp_path / "visible").mkdir()
    base = str(tmp_path) + os.sep
    assert not any(".oculto" in m for m in directory_matches(base))
    assert any(".oculto" in m for m in directory_matches(base + "."))
    assert directory_matches(str(tmp_path / "no-existe") + os.sep) == []


def test_directory_matches_expands_tilde():
    home = os.path.expanduser("~")
    assert os.path.isdir(home)
    matches = directory_matches("~" + os.sep)
    assert all(m.startswith(home) for m in matches)


def test_picker_smoke_in_new_task_dialog(tmp_path):
    """The dropdown appears when typing and Enter fixes the path."""
    (tmp_path / "alfa").mkdir()

    async def scenario():
        from grafeno.app import GrafenoApp
        from grafeno.tui.screens.tasks import NewTaskScreen
        from textual.widgets import Input, OptionList

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)

            workdir = app.screen.query_one("#nt-workdir", Input)
            workdir.value = str(tmp_path) + os.sep
            await pilot.pause()

            options = app.screen.query_one("#dir-options", OptionList)
            assert options.option_count == 1

            workdir.focus()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert workdir.value == str(tmp_path / "alfa") + os.sep

    asyncio.run(scenario())
