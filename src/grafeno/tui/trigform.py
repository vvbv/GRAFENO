"""Reusable global-triggers editor: table of triggers + add/delete form."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static

from ..i18n import t
from ..triggers import ALL_PHASES, TRIGGER_STAGES, TIMINGS, Trigger


class TriggersForm(Static):
    """List of triggers with inputs to add and a button to delete."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._triggers: list[Trigger] = []

    def compose(self) -> ComposeResult:
        yield DataTable(id="trig-table", classes="refs-table")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("trig.name"), id="trig-name")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("trig.description"), id="trig-description")
        with Horizontal(classes="automode-row"):
            yield Input(placeholder=t("trig.workdir"), id="trig-workdir")
        with Horizontal(classes="automode-row"):
            yield Select(
                [(t("trig.timing.after"), "after"),
                 (t("trig.timing.before"), "before")],
                id="trig-timing",
                value="after",
                allow_blank=False,
            )
            yield Checkbox(t("trig.all_phases"), id="trig-phase-all", value=True)
        with Horizontal(classes="automode-row", id="trig-stages"):
            for stage in TRIGGER_STAGES:
                yield Checkbox(t(f"hook.stage.{stage}"), id=f"trig-stage-{stage}")
        with Horizontal(classes="automode-row"):
            yield Button(t("refs.add"), id="trig-add")
            yield Button(t("refs.delete"), id="trig-delete")

    def on_mount(self) -> None:
        table = self.query_one("#trig-table", DataTable)
        table.add_columns(
            t("refs.col.name"), t("trig.col.phases"), t("trig.col.timing")
        )

    # ------------------------------------------------------------------ #
    def set_triggers(self, triggers: list[Trigger]) -> None:
        """Replace the edited list (e.g. when loading the screen)."""
        self._triggers = list(triggers)
        self._refresh()

    def triggers(self) -> list[Trigger]:
        """Current edited list (a copy)."""
        return list(self._triggers)

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        table = self.query_one("#trig-table", DataTable)
        table.clear()
        for index, trigger in enumerate(self._triggers):
            table.add_row(
                trigger.name,
                trigger.phases,
                t(f"trig.timing.{trigger.timing}"),
                key=str(index),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "trig-add":
            self._add()
            event.stop()
        elif event.button.id == "trig-delete":
            self._delete_selected()
            event.stop()

    def _add(self) -> None:
        name = self.query_one("#trig-name", Input).value.strip()
        if not name:
            self.notify(t("trig.error.name_required"), severity="error")
            return
        description = self.query_one("#trig-description", Input).value.strip()
        workdir = self.query_one("#trig-workdir", Input).value.strip()
        timing = str(self.query_one("#trig-timing", Select).value)
        if self.query_one("#trig-phase-all", Checkbox).value:
            phases = ALL_PHASES
        else:
            chosen = [
                stage
                for stage in TRIGGER_STAGES
                if self.query_one(f"#trig-stage-{stage}", Checkbox).value
            ]
            if not chosen:
                self.notify(t("trig.error.phases_required"), severity="error")
                return
            phases = ",".join(chosen)
        self._triggers.append(
            Trigger(
                name=name,
                description=description,
                phases=phases,
                timing=timing if timing in TIMINGS else "after",
                workdir=workdir,
            )
        )
        for input_id in ("#trig-name", "#trig-description", "#trig-workdir"):
            self.query_one(input_id, Input).value = ""
        for stage in TRIGGER_STAGES:
            self.query_one(f"#trig-stage-{stage}", Checkbox).value = False
        self.query_one("#trig-phase-all", Checkbox).value = True
        self._refresh()

    def _delete_selected(self) -> None:
        table = self.query_one("#trig-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key.value is None:
            return
        del self._triggers[int(str(row_key.value))]
        self._refresh()
