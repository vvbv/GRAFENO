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


def test_picker_placeholder_and_no_suggestions_when_empty():
    """``suggest_empty=False`` keeps the dropdown hidden while the input is empty."""
    from textual.app import App
    from textual.widgets import Input, OptionList

    from grafeno.tui.dirpicker import DirectoryPicker

    class Holder(App):
        def compose(self):
            yield DirectoryPicker("", input_id="pick", placeholder="where", suggest_empty=False)

    async def scenario():
        app = Holder()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#pick", Input).placeholder == "where"
            assert app.query_one("#dir-options", OptionList).option_count == 0

    asyncio.run(scenario())


def test_directory_matches_include_files(tmp_path):
    import os

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.xlsx").write_bytes(b"x")
    base = str(tmp_path) + os.sep
    assert str(tmp_path / "a.xlsx") not in directory_matches(base)
    found = directory_matches(base, include_files=True)
    assert str(tmp_path / "a.xlsx") in found
    assert str(tmp_path / "sub") + os.sep in found
