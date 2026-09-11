"""Modal for creating or editing a processing profile.

Cloned from ``screens/roles.py`` and extended with a name ``Input`` plus
the ``existing_names`` set used to block duplicates on save. The model
loading worker is cancelable with Esc (same convention as the task agents
modal): the first Esc cancels the load, the second closes without saving.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ... import config as config_module
from ...config import KNOWN_CLIS, RoleConfig
from ...drivers import fetch_all_models, fetch_all_variants
from ...i18n import t
from ...profiles import PROFILE_ROLES, Profile
from ..rolesform import ROLES, RolesForm


class ProfileEditScreen(ModalScreen[Profile | None]):
    """Returns the edited Profile, or ``None`` if the user cancelled."""

    BINDINGS = [Binding("escape", "cancel", t("common.cancel"))]

    def __init__(
        self,
        profile: Profile | None = None,
        existing_names: set[str] | None = None,
    ):
        super().__init__()
        self._profile = profile
        self._existing_names: set[str] = existing_names or set()
        self._loading = False

    def compose(self) -> ComposeResult:
        with Vertical(id="new-task-dialog", classes="roles-dialog"):
            title_key = (
                "profedit.title_edit"
                if self._profile is not None
                else "profedit.title_new"
            )
            title_kwargs = {} if self._profile is None else {"name": self._profile.name}
            yield Label(t(title_key, **title_kwargs), id="new-task-title")
            yield Static(t("roles.body"), classes="pc-detail")
            with Horizontal(classes="automode-row"):
                yield Label(t("profedit.name"))
                yield Input(id="pe-name")
            yield RolesForm()
            yield Static(t("cfg.models.loading"), id="roles-status")
            with Horizontal(id="nt-buttons"):
                yield Button(t("common.save"), variant="primary", id="pe-save")
                yield Button(t("common.cancel"), id="pe-cancel")

    def on_mount(self) -> None:
        cfg = config_module.load()
        # Initial values: the profile under edit, or the global Config
        # defaults (so a new profile starts from a useful state).
        defaults: dict[str, RoleConfig] = {
            role: (
                getattr(self._profile, role)
                if self._profile is not None
                else getattr(cfg, role)
            )
            for role in PROFILE_ROLES
        }
        name_input = self.query_one("#pe-name", Input)
        name_input.value = "" if self._profile is None else self._profile.name
        form = self.query_one(RolesForm)
        for role in PROFILE_ROLES:
            cfg_role = defaults[role]
            form.set_role(role, cfg_role.cli, cfg_role.model, cfg_role.effort)
        self._load_models()

    # ------------------------------------------------------------------ #
    # Model loading (async worker: cancelable with Esc)
    # ------------------------------------------------------------------ #
    def _load_models(self) -> None:
        self._loading = True
        self._models_worker = self.run_worker(
            self._fetch_models,
            exclusive=True,
            group="profile-models",
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
        if self._cancel_loading():
            return
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pe-cancel":
            self._cancel_loading()
            self.dismiss(None)
            return
        self._save()

    def _save(self) -> None:
        self._cancel_loading()
        name = self.query_one("#pe-name", Input).value.strip()
        if not name:
            self.notify(t("prof.error.name_required"), severity="error")
            return
        if name in self._existing_names:
            self.notify(t("prof.error.duplicate", name=name), severity="error")
            return
        form = self.query_one(RolesForm)
        edited = Profile(name=name)
        for role, _ in ROLES:
            setattr(edited, role, RoleConfig(*form.role_values(role)))
        self.dismiss(edited)