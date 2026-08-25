"""Reusable references editor: table of references + add/delete form.

Used by the global settings screen (edits ``~/.grafeno/references.toml``) and
by the new-task modal (task-level references).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Static

from ..i18n import t
from ..references import Reference


class ReferencesForm(Static):
    """List of references with inputs to add and a button to delete."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._references: list[Reference] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="refs-table", classes="refs-table")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("refs.name"), id="refs-name")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("refs.description"), id="refs-description")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("refs.path"), id="refs-path")
        with Horizontal(classes="automode-row"):
            yield Button(t("refs.add"), id="refs-add")
            yield Button(t("refs.delete"), id="refs-delete")

    def on_mount(self) -> None:
        table = self.query_one("#refs-table", DataTable)
        table.add_columns(t("refs.col.name"), t("refs.col.path"))

    # ------------------------------------------------------------------ #
    # Values
    # ------------------------------------------------------------------ #
    def set_references(self, references: list[Reference]) -> None:
        """Replace the edited list (e.g. when loading the screen)."""
        self._references = list(references)
        self._refresh()

    def references(self) -> list[Reference]:
        """Current edited list (a copy)."""
        return list(self._references)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#refs-table", DataTable)
        table.clear()
        for index, ref in enumerate(self._references):
            table.add_row(ref.name, ref.path, key=str(index))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refs-add":
            self._add()
        elif event.button.id == "refs-delete":
            self._delete_selected()

    def _add(self) -> None:
        name = self.query_one("#refs-name", Input).value.strip()
        description = self.query_one("#refs-description", Input).value.strip()
        path = self.query_one("#refs-path", Input).value.strip()
        if not name:
            self.notify(t("refs.error.name_required"), severity="error")
            return
        if not path:
            self.notify(t("refs.error.path_required"), severity="error")
            return
        self._references.append(
            Reference(name=name, description=description, path=path)
        )
        self.query_one("#refs-name", Input).value = ""
        self.query_one("#refs-description", Input).value = ""
        self.query_one("#refs-path", Input).value = ""
        self._refresh()

    def _delete_selected(self) -> None:
        table = self.query_one("#refs-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return
        del self._references[int(str(row_key.value))]
        self._refresh()
