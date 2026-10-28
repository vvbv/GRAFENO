"""Reusable startup-commands editor: table of commands + preset/add/delete form."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from .. import startup
from ..config import StartupAction
from ..i18n import t

_TEXT_INPUTS = (
    "#startup-name", "#startup-command", "#startup-workdir",
    "#startup-settings-file", "#startup-settings", "#startup-open-url",
)


class StartupForm(Static):
    """List of startup commands with inputs to add, presets and a delete button."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._actions: list[StartupAction] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="startup-table", classes="refs-table")
        with Horizontal(classes="automode-row"):
            yield Select(
                [(t(f"cfg.startup.preset.{key}"), key) for key in startup.PRESETS],
                prompt=t("cfg.startup.preset"),
                id="startup-preset",
            )
        for input_id, key in (
            ("startup-name", "name"), ("startup-command", "command"),
            ("startup-workdir", "workdir"), ("startup-settings-file", "settings_file"),
            ("startup-settings", "settings"), ("startup-open-url", "open_url"),
        ):
            with Horizontal(classes="automode-row"):
                yield Input(placeholder=t(f"cfg.startup.{key}"), id=input_id)
        with Horizontal(classes="automode-row"):
            yield Checkbox(t("cfg.startup.enabled"), id="startup-enabled", value=True)
            yield Checkbox(t("cfg.startup.stop_on_exit"), id="startup-stop", value=True)
        with Horizontal(classes="automode-row"):
            yield Button(t("refs.add"), id="startup-add")
            yield Button(t("refs.delete"), id="startup-delete")

    def on_mount(self) -> None:
        table = self.query_one("#startup-table", DataTable)
        table.add_columns(
            t("refs.col.name"), t("cfg.startup.command"),
            t("cfg.startup.enabled"), t("cfg.startup.stop_on_exit"),
        )
        self._refresh()

    # ------------------------------------------------------------------ #
    def set_actions(self, actions: list[StartupAction]) -> None:
        """Replace the edited list (e.g. when loading the screen)."""
        self._actions = list(actions)
        self._refresh()

    def actions(self) -> list[StartupAction]:
        """Current edited list (a copy)."""
        return list(self._actions)

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#startup-table", DataTable)
        table.clear()
        for index, action in enumerate(self._actions):
            table.add_row(
                action.name,
                action.command,
                t("cfg.startup.yes" if action.enabled else "cfg.startup.no"),
                t("cfg.startup.yes" if action.stop_on_exit else "cfg.startup.no"),
                key=str(index),
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "startup-preset":
            return
        event.stop()
        if event.value is Select.BLANK:
            return
        chosen = startup.preset(str(event.value))
        if chosen is None:
            return
        self.query_one("#startup-name", Input).value = chosen.name
        self.query_one("#startup-command", Input).value = chosen.command
        self.query_one("#startup-workdir", Input).value = ""
        self.query_one("#startup-settings-file", Input).value = chosen.settings_file
        self.query_one("#startup-settings", Input).value = chosen.settings
        self.query_one("#startup-open-url", Input).value = chosen.open_url
        self.query_one("#startup-enabled", Checkbox).value = True
        self.query_one("#startup-stop", Checkbox).value = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "startup-add":
            self._add()
            event.stop()
        elif event.button.id == "startup-delete":
            self._delete_selected()
            event.stop()

    def _add(self) -> None:
        action = StartupAction(
            name=self.query_one("#startup-name", Input).value.strip(),
            command=self.query_one("#startup-command", Input).value.strip(),
            enabled=self.query_one("#startup-enabled", Checkbox).value,
            workdir=self.query_one("#startup-workdir", Input).value.strip(),
            stop_on_exit=self.query_one("#startup-stop", Checkbox).value,
            settings_file=self.query_one("#startup-settings-file", Input).value.strip(),
            settings=self.query_one("#startup-settings", Input).value.strip(),
            open_url=self.query_one("#startup-open-url", Input).value.strip(),
        )
        error = startup.validation_error(action)
        if error:
            self.notify(error, severity="error")
            return
        self._actions.append(action)
        for input_id in _TEXT_INPUTS:
            self.query_one(input_id, Input).value = ""
        self.query_one("#startup-enabled", Checkbox).value = True
        self.query_one("#startup-stop", Checkbox).value = True
        self.query_one("#startup-preset", Select).clear()
        self._refresh()

    def _delete_selected(self) -> None:
        table = self.query_one("#startup-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return
        del self._actions[int(str(row_key.value))]
        self._refresh()
