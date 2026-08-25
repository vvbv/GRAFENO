"""Modal for configuring the agents (CLI + model) of a specific task.

Edits the planner/implementer/reviewer roles of the task and persists them
in ``task.toml``. Model loading is cancelable with Esc (first Esc cancels
the load; the second one closes without saving).
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ... import models
from ...config import KNOWN_CLIS
from ...drivers import fetch_all_models, fetch_all_variants
from ...i18n import t
from ...models import Task
from ..rolesform import ROLES, RolesForm


class TaskRolesScreen(ModalScreen[bool]):
    """Returns True if changes were saved to the task."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(self, task: Task):
        super().__init__()
        self._gtask = task
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog", classes="roles-dialog"):
            yield Label(t("roles.title", name=self._gtask.name), id="new-task-title")
            yield Static(
                t("roles.body"),
                classes="pc-detail",
            )
            yield RolesForm()
            yield Static(t("cfg.models.loading"), id="roles-status")
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.save"), variant="primary", id="tr-save")
                yield Button(t("common.cancel"), id="tr-cancel")

    def on_mount(self) -> None:
        form = self.query_one(RolesForm)
        for role, _ in ROLES:
            role_cfg = self._gtask.role(role)
            form.set_role(role, role_cfg.cli, role_cfg.model, role_cfg.effort)
        self._load_models()

    # ------------------------------------------------------------------ #
    # Model loading (async worker: cancelable with Esc)
    # ------------------------------------------------------------------ #
    def _load_models(self) -> None:
        self._loading = True
        self._models_worker = self.run_worker(
            self._fetch_models,
            exclusive=True,
            group="task-roles-models",
            exit_on_error=False,
        )

    async def _fetch_models(self) -> None:
        models_map = await fetch_all_models(KNOWN_CLIS)
        variants_map = await fetch_all_variants(KNOWN_CLIS)
        self._apply_models(models_map, variants_map)

    def _apply_models(
        self,
        models_map: dict[str, list[str]],
        variants_map: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        self._loading = False
        form = self.query_one(RolesForm)
        form.set_models(models_map)
        if variants_map is not None:
            form.set_variants(variants_map)
        summary = " · ".join(
            t("cfg.models.count", cli=cli, count=len(models)) if models else t("cfg.models.unavailable", cli=cli)
            for cli, models in models_map.items()
        )
        self.query_one("#roles-status", Static).update(summary)

    def _cancel_loading(self) -> bool:
        """Cancel model loading if still in progress. True if cancelled."""
        if self._loading:
            self._models_worker.cancel()
            self._loading = False
            self.query_one("#roles-status", Static).update(t("cfg.models.canceled"))
            return True
        return False

    # ------------------------------------------------------------------ #
    def action_cancel(self) -> None:
        # With a load in progress, the first Esc cancels it; the second closes.
        if self._cancel_loading():
            return
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "tr-cancel":
            self._cancel_loading()
            self.dismiss(False)
            return
        self._save()

    def _save(self) -> None:
        self._cancel_loading()
        form = self.query_one(RolesForm)
        for role, _ in ROLES:
            role_cfg = self._gtask.role(role)
            role_cfg.cli, role_cfg.model, role_cfg.effort = form.role_values(role)
        models.save(self._gtask)
        self.notify(t("roles.saved"))
        self.dismiss(True)
