"""Reusable processing-profiles editor: table + add/edit/delete buttons.

Used by the global settings screen to edit ``~/.grafeno/profiles.toml``.
Editing and creating rows open ``ProfileEditScreen`` (name + RolesForm).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static

from ..i18n import t
from ..profiles import Profile
from .screens.profileedit import ProfileEditScreen


class ProfilesForm(Static):
    """List of profiles with add/edit/delete buttons."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._profiles: list[Profile] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="prof-table", classes="refs-table")
        with Horizontal(classes="automode-row"):
            yield Button(t("refs.add"), id="prof-add")
            yield Button(t("prof.edit"), id="prof-edit")
            yield Button(t("refs.delete"), id="prof-delete")

    def on_mount(self) -> None:
        table = self.query_one("#prof-table", DataTable)
        table.add_columns(t("refs.col.name"), t("prof.col.roles"))

    # ------------------------------------------------------------------ #
    def set_profiles(self, profiles: list[Profile]) -> None:
        """Replace the edited list (e.g. when loading the screen)."""
        self._profiles = list(profiles)
        self._refresh()

    def profiles(self) -> list[Profile]:
        """Current edited list (a copy)."""
        return list(self._profiles)

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#prof-table", DataTable)
        table.clear()
        for index, profile in enumerate(self._profiles):
            table.add_row(
                profile.name,
                profile.summary(),
                key=str(index),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prof-add":
            self._open_editor(None)
            event.stop()
            return
        if event.button.id == "prof-edit":
            self._edit_selected()
            event.stop()
            return
        if event.button.id == "prof-delete":
            self._delete_selected()
            event.stop()
            return

    def _selected_index(self) -> int:
        """Index of the table's cursor row, or -1 when there is none."""
        table = self.query_one("#prof-table", DataTable)
        if table.row_count == 0:
            return -1
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return -1
        return int(str(row_key.value))

    def _edit_selected(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        self._open_editor(self._profiles[index])

    def _delete_selected(self) -> None:
        index = self._selected_index()
        if index < 0:
            return
        del self._profiles[index]
        self._refresh()

    # ------------------------------------------------------------------ #
    def _open_editor(self, profile: Profile | None) -> None:
        """Open the edit modal; apply the result on close."""
        editing_index = (
            self._profiles.index(profile) if profile in self._profiles else -1
        )
        existing = {
            item.name
            for i, item in enumerate(self._profiles)
            if i != editing_index
        }

        def closed(result: Profile | None) -> None:
            if result is None:
                return
            self._apply_edit(editing_index, result)

        self.app.push_screen(
            ProfileEditScreen(profile, existing),
            closed,
        )

    def _apply_edit(self, index: int, result: Profile) -> None:
        """Insert or replace one row using the index captured before the modal."""
        if index >= 0:
            self._profiles[index] = result
        else:
            self._profiles.append(result)
        self._refresh()