"""Reusable working-directories editor: table of root folders + add/delete.

Used by the global settings screen (``Config.workspaces``). Every entry is a
root folder whose first-level subfolders are listed as projects; the second
column shows how many were found (or that the folder does not exist). The
add input autocompletes folders (``DirectoryPicker``) and Enter adds it.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Static

from .. import remotesession
from .. import workspaces as workspaces_module
from ..i18n import t
from .dirpicker import DirectoryPicker


class WorkspacesForm(Static):
    """Editable list of working directories (root folders of the projects)."""

    BINDINGS = [Binding("delete", "delete_selected", t("refs.delete"), show=False)]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._entries: list[str] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="ws-table", classes="refs-table", cursor_type="row")
        yield Static(t("cfg.workspaces.empty"), id="ws-empty")
        yield DirectoryPicker(
            "",
            input_id="ws-path",
            placeholder=t("cfg.workspaces.placeholder"),
            suggest_empty=False,
        )
        with Horizontal(classes="automode-row"):
            yield Button(t("refs.add"), id="ws-add")
            yield Button(t("refs.delete"), id="ws-delete")

    def on_mount(self) -> None:
        self.query_one("#ws-table", DataTable).add_columns(
            t("cfg.workspaces.col.path"), t("cfg.workspaces.col.state")
        )
        self._refresh()

    # ------------------------------------------------------------------ #
    # Values
    # ------------------------------------------------------------------ #
    def set_workspaces(self, entries: list[str]) -> None:
        """Replace the edited list (e.g. when loading the screen)."""
        self._entries = workspaces_module.normalize_entries(entries)
        self._refresh()

    def workspaces(self) -> list[str]:
        """Edited list plus a folder still typed in the add input (a copy)."""
        pending = self.query_one("#ws-path", Input).value
        return workspaces_module.normalize_entries([*self._entries, pending])

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#ws-table", DataTable)
        table.clear()
        for index, entry in enumerate(self._entries):
            table.add_row(Text(entry), self._state(entry), key=str(index))
        self.query_one("#ws-empty", Static).display = not self._entries

    @staticmethod
    def _root(entry: str) -> workspaces_module.Root | None:
        """State of ``entry`` on disk; None in session mode (remote folder)."""
        if remotesession.active():
            return None
        found = workspaces_module.roots([entry])
        return found[0] if found else workspaces_module.Root(entry=entry, path="", exists=False)

    @classmethod
    def _state(cls, entry: str) -> str:
        """Second column: projects found in ``entry`` or "not found"."""
        root = cls._root(entry)
        if root is None:
            return ""
        if not root.exists:
            return t("cfg.workspaces.state.missing")
        count = len(workspaces_module.discover([Path(root.path)]))
        return t("cfg.workspaces.state.count", count=count)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Always stop: ConfigScreen saves on any button it does not know.
        if event.button.id == "ws-add":
            event.stop()
            self._add()
        elif event.button.id == "ws-delete":
            event.stop()
            self._delete_selected()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "ws-path":
            event.stop()
            self._add()

    def action_delete_selected(self) -> None:
        self._delete_selected()

    def _add(self) -> None:
        path_input = self.query_one("#ws-path", Input)
        raw = path_input.value.strip()
        if not raw:
            self.notify(t("cfg.workspaces.error.empty"), severity="error")
            return
        entry = workspaces_module.normalize_entries([raw])[0]
        known = {workspaces_module.entry_key(item) for item in self._entries}
        if workspaces_module.entry_key(entry) in known:
            self.notify(t("cfg.workspaces.error.duplicate"), severity="warning")
            return
        root = self._root(entry)
        if root is not None and not root.exists:
            self.notify(t("cfg.workspaces.warn_missing", path=entry), severity="warning")
        self._entries.append(entry)
        path_input.value = ""
        self._refresh()

    def _delete_selected(self) -> None:
        table = self.query_one("#ws-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return
        del self._entries[int(str(row_key.value))]
        self._refresh()
