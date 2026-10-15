"""Projects screen: folders found in the working directories (workspaces).

Every first-level subfolder of a configured working directory is a project.
The screen lists them with their task count and a text filter; Enter (or a
click on the highlighted row) opens the project actions and "new task"
dismisses the screen with the project folder, so the task list opens the
new-task form with that folder preset.
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from ... import config as config_module
from ... import models, remotesession
from ... import workspaces as workspaces_module
from ...i18n import t
from ..widgets import GrafenoHeader, LocationBar
from .config import ConfigScreen

NEW_TASK = "new_task"  # ProjectActionsScreen result: create a task in the project


class ProjectActionsScreen(ModalScreen[str | None]):
    """Actions offered for one project (for now: create a task in it)."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, project: workspaces_module.Project) -> None:
        super().__init__()
        self._project = project

    def compose(self) -> ComposeResult:
        with Vertical(id="project-actions-dialog"):
            yield Label(
                t("projects.actions.title", name=self._project.name),
                id="project-actions-title",
                markup=False,
            )
            yield Static(str(self._project.path), classes="pc-detail", markup=False)
            with Horizontal(id="pa-buttons"):
                yield Button(t("projects.action.new_task"), variant="primary", id="pa-new-task")
                yield Button(t("common.cancel"), id="pa-cancel")

    def on_mount(self) -> None:
        self.query_one("#pa-new-task", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(NEW_TASK if event.button.id == "pa-new-task" else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProjectsScreen(Screen[str | None]):
    """Projects of the working directories, with task counts and a filter.

    Dismissed with the folder of the chosen project when the user asks for
    a new task in it (``None`` = back without choosing).
    """

    BINDINGS = [
        Binding("escape", "back", t("common.back")),
        Binding("n", "new_task", t("tasks.bind.new")),
        Binding("f", "focus_filter", t("projects.bind.filter")),
        Binding("w", "workspaces", t("projects.bind.workspaces")),
        Binding("r", "reload", t("tasks.bind.reload")),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._roots: list[workspaces_module.Root] = []
        self._projects: list[workspaces_module.Project] = []
        self._shown: list[workspaces_module.Project] = []
        self._counts: dict[str, int] = {}

    def compose(self) -> ComposeResult:
        yield GrafenoHeader()
        yield LocationBar(id="location-bar")
        with Horizontal(id="projects-header"):
            yield Static(t("projects.subtitle"), id="subtitle")
            yield Input(placeholder=t("projects.filter.placeholder"), id="projects-filter")
            yield Button(t("projects.edit_workspaces"), id="projects-workspaces", compact=True)
        yield DataTable(id="projects-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="projects-hint", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.add_columns(
            t("projects.col.name"), t("projects.col.tasks"), t("projects.col.path")
        )
        self._reload()
        table.focus()

    def on_screen_resume(self) -> None:
        # Back from the settings or the actions dialog: refresh the listing.
        self._reload()

    # ------------------------------------------------------------ data #
    def _reload(self) -> None:
        """Re-read the working directories, their folders and the task counts."""
        self._roots, self._projects = [], []
        if not remotesession.active():
            entries = workspaces_module.effective_entries(
                config_module.load().workspaces, Path.cwd()
            )
            self._roots = workspaces_module.roots(entries)
            self._projects = workspaces_module.projects(
                [Path(root.path) for root in self._roots if root.exists]
            )
        self._counts = {}
        for task in models.list_all():
            if task.workdir and not task.is_remote:
                self._counts[task.workdir] = self._counts.get(task.workdir, 0) + 1
        self._fill_table()

    def _fill_table(self) -> None:
        """Fill the table with the projects matching the filter (keeps the cursor)."""
        table = self.query_one("#projects-table", DataTable)
        current = self._highlighted()
        needle = self.query_one("#projects-filter", Input).value.strip().casefold()
        self._shown = [
            project
            for project in self._projects
            if not needle
            or needle in project.name.casefold()
            or needle in str(project.path).casefold()
        ]
        table.clear()
        for index, project in enumerate(self._shown):
            table.add_row(
                Text(project.name),
                str(self._counts.get(str(project.path), 0)),
                Text(str(project.path)),
                key=str(index),
            )
            if current is not None and project.path == current.path:
                table.move_cursor(row=index, scroll=False)
        self.query_one("#projects-hint", Static).update(self._hint())

    def _hint(self) -> str:
        """Status under the table: why it is empty, missing roots or counts."""
        if remotesession.active():
            return t("projects.session")
        if not self._roots:
            return t("projects.empty.no_workspaces")
        lines: list[str] = []
        missing = [root.entry for root in self._roots if not root.exists]
        if missing:
            lines.append(t("projects.missing", paths=", ".join(missing)))
        if not self._projects:
            lines.append(t("projects.empty.no_projects"))
        elif not self._shown:
            lines.append(t("projects.empty.filter"))
        else:
            lines.append(t("projects.count", shown=len(self._shown), total=len(self._projects)))
            lines.append(t("projects.hint"))
        return "\n".join(lines)

    def _highlighted(self) -> workspaces_module.Project | None:
        """Project under the table cursor (None when the table is empty)."""
        table = self.query_one("#projects-table", DataTable)
        if table.row_count == 0:
            return None
        row = table.cursor_row
        return self._shown[row] if 0 <= row < len(self._shown) else None

    # ------------------------------------------------------------ events #
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "projects-filter":
            self._fill_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "projects-filter":
            self.query_one("#projects-table", DataTable).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "projects-workspaces":
            self.action_workspaces()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        project = self._highlighted()
        if project is not None:
            self._ask_action(project)

    def _ask_action(self, project: workspaces_module.Project) -> None:
        """Open the actions dialog; "new task" dismisses with the project folder."""

        def chosen(action: str | None) -> None:
            if action == NEW_TASK:
                self.dismiss(str(project.path))

        self.app.push_screen(ProjectActionsScreen(project), chosen)

    # ------------------------------------------------------------ actions #
    def action_new_task(self) -> None:
        """Shortcut: new task in the highlighted project (no actions dialog)."""
        project = self._highlighted()
        if project is not None:
            self.dismiss(str(project.path))

    def action_focus_filter(self) -> None:
        self.query_one("#projects-filter", Input).focus()

    def action_workspaces(self) -> None:
        """Open the settings scrolled to the working-directories section."""
        self.app.push_screen(ConfigScreen(section="workspaces"))

    def action_reload(self) -> None:
        self._reload()

    def action_back(self) -> None:
        self.dismiss(None)
