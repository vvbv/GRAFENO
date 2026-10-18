"""Reusable attachments editor: local file paths to copy into a task.

Used by the new-task modal and the "ask for more" modal; the files are only
copied (never loaded in memory) when the caller invokes ``save_into`` (after the task exists).
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Input, Static

from .. import media
from ..i18n import t
from .dirpicker import DirectoryPicker


class AttachmentsForm(Static):
    """List of local files to attach, with a path picker and add/remove buttons."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._paths: list[Path] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="att-table", classes="refs-table")
        with Horizontal(classes="automode-row"):
            yield DirectoryPicker(
                "", input_id="att-path", placeholder=t("media.att.path"),
                suggest_empty=False, include_files=True,
            )
        with Horizontal(classes="automode-row"):
            yield Button(t("media.att.add"), id="att-add")
            yield Button(t("media.att.delete"), id="att-delete")

    def on_mount(self) -> None:
        table = self.query_one("#att-table", DataTable)
        table.add_columns(t("media.att.col.name"), t("media.att.col.size"))

    # ------------------------------------------------------------------ #
    # Values
    # ------------------------------------------------------------------ #
    def paths(self) -> list[Path]:
        """Files queued for attaching (a copy)."""
        return list(self._paths)

    def save_into(self, task_id: str, *, origin: str, cycle: int) -> tuple[int, list[str]]:
        """Copy the queued files into the task media dir.

        Returns (saved count, names of the files that could not be saved).
        """
        saved = 0
        failed: list[str] = []
        self._add()  # a path typed but never confirmed with "Attach" is not lost
        for path in self._paths:
            # copied in-kernel (shutil.copyfile): the file is never loaded in memory
            if media.save_attachment_file(task_id, path.name, path, origin=origin, cycle=cycle) is None:
                failed.append(path.name)
            else:
                saved += 1
        return saved, failed

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#att-table", DataTable)
        table.clear()
        for index, path in enumerate(self._paths):
            try:
                size = media.format_size(path.stat().st_size)
            except OSError:
                size = "?"
            table.add_row(path.name, size, key=str(index))

    def _add(self) -> None:
        picker = self.query_one("#att-path", Input)
        value = picker.value.strip()
        if not value:
            return
        path = Path(value).expanduser()
        if not path.is_file():
            self.notify(t("media.att.not_found", path=value), severity="error")
            return
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size > media.MAX_ATTACHMENT_BYTES:
            self.notify(
                t(
                    "media.att.too_large",
                    name=path.name,
                    limit=media.format_size(media.MAX_ATTACHMENT_BYTES),
                ),
                severity="error",
            )
            return
        resolved = path.resolve()
        if all(existing.resolve() != resolved for existing in self._paths):
            self._paths.append(path)
        picker.value = ""
        self._refresh()

    def _delete_selected(self) -> None:
        table = self.query_one("#att-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return
        del self._paths[int(str(row_key.value))]
        self._refresh()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        # Stop the event: parent modals treat any non-cancel button as "accept".
        if event.button.id == "att-add":
            event.stop()
            self._add()
        elif event.button.id == "att-delete":
            event.stop()
            self._delete_selected()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "att-path":
            event.stop()
            self._add()
