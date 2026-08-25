"""Directory selector with autocomplete for the TUI."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option


def directory_matches(value: str, *, limit: int = 15) -> list[str]:
    """Return paths of existing directories that complete ``value``.

    Rules:
    - Directories only (never files).
    - ``~`` expands to the user's home.
    - If ``value`` ends in a separator, list the children of that directory.
    - Otherwise complete the last segment by prefix (case-insensitive).
    - Hidden directories only appear if the prefix starts with ``.``.
    - Returned paths preserve the typed shape (not resolved) and end with
      a separator so the user can keep going deeper.
    - Permission errors or non-existent paths return an empty list.
    """
    raw = value if value else "."
    expanded = os.path.expanduser(raw)
    if expanded.endswith(os.sep):
        base, prefix = expanded, ""
    else:
        head, tail = os.path.split(expanded)
        base, prefix = (head or "."), tail
    base_path = Path(base)
    if not base_path.is_dir():
        return []
    try:
        entries = list(base_path.iterdir())
    except OSError:
        return []
    prefix_fold = prefix.casefold()
    matches = [
        e
        for e in entries
        if e.is_dir()
        and e.name.casefold().startswith(prefix_fold)
        and (prefix.startswith(".") or not e.name.startswith("."))
    ]
    matches.sort(key=lambda e: e.name.casefold())
    return [str(e) + os.sep for e in matches[:limit]]


class DirectoryPicker(Widget):
    """Path input with a dropdown list of candidate directories.

    The inner Input carries the id given by the caller (e.g. ``nt-workdir``);
    the dropdown is shown while typing and is filled with
    ``directory_matches``. Keys: down-arrow (from the input) enters the list;
    Enter/click selects; Escape (in the list) returns to the input. Tab
    changes focus as usual.
    """

    DEFAULT_CSS = """
    DirectoryPicker {
        height: auto;
    }
    DirectoryPicker OptionList {
        height: auto;
        max-height: 8;
        display: none;
    }
    DirectoryPicker OptionList.visible {
        display: block;
    }
    """

    class Changed(Message):
        """Forwards the inner Input's changes (bubbles like Input.Changed)."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, value: str = "", *, input_id: str) -> None:
        super().__init__()
        self._input_id = input_id
        self._initial = value
        self._matches: list[str] = []

    @property
    def value(self) -> str:
        return self.query_one(Input).value

    @value.setter
    def value(self, new_value: str) -> None:
        self.query_one(Input).value = new_value

    def compose(self) -> ComposeResult:
        yield Input(value=self._initial, id=self._input_id)
        yield OptionList(id="dir-options")

    def on_mount(self) -> None:
        self._refresh_options()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != self._input_id:
            return
        self._refresh_options()
        self.post_message(self.Changed(event.value))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter or click on an option: fix the path and return to the input.
        self._pick(self._matches[event.option_index])

    def _pick(self, path: str) -> None:
        input_widget = self.query_one(Input)
        input_widget.value = path
        input_widget.cursor_position = len(path)
        input_widget.focus()
        # _refresh_options() is triggered via Input.Changed and shows the children.

    def _refresh_options(self) -> None:
        options = self.query_one("#dir-options", OptionList)
        options.clear_options()
        self._matches = directory_matches(self.query_one(Input).value)
        for path in self._matches:
            options.add_option(Option(path))
        options.set_class(bool(options.option_count), "visible")

    def on_key(self, event) -> None:
        options = self.query_one("#dir-options", OptionList)
        if event.key == "down" and self.query_one(Input).has_focus and options.option_count:
            options.focus()
            options.highlighted = 0
            event.stop()
        elif event.key == "escape" and options.has_focus:
            self.query_one(Input).focus()
            event.stop()
