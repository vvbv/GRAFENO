"""Reusable pipeline roles form (CLI + model + effort per role).

Used by the global settings screen and by the per-task settings modal.
The model and variant loading is done by the container (screen), which
calls ``set_models`` and ``set_variants`` when ``fetch_all_models`` and
``fetch_all_variants`` finish.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Label, Select, Static

from ..config import KNOWN_CLIS
from ..i18n import t

# Pipeline roles (id, i18n key for the title).
ROLES: tuple[tuple[str, str], ...] = (
    ("planner", "cfg.role.planner"),
    ("implementer", "cfg.role.implementer"),
    ("reviewer", "cfg.role.reviewer"),
    ("final", "cfg.role.final"),
)

MODEL_PROMPT = t("cfg.model.prompt")
MODEL_FILTER_PROMPT = t("cfg.model.filter")
EFFORT_PROMPT = t("cfg.effort.prompt")


def filter_models(models: list[str], query: str) -> list[str]:
    """Return the models that contain ``query`` (case-insensitive).

    An empty query returns the full list (a copy). It is a substring, not a
    prefix, so e.g. ``k3`` can be found inside ``opencode-go/kimi-k3``.
    """
    needle = query.strip().casefold()
    if not needle:
        return list(models)
    return [m for m in models if needle in m.casefold()]


class RoleRow(Static):
    """Configuration row for a role: CLI Select + model + effort."""

    def __init__(self, role: str, title_key: str):
        super().__init__(classes="role-row")
        self.role = role
        self._title_key = title_key

    def compose(self) -> ComposeResult:
        yield Label(t(self._title_key), classes="role-title")
        with Horizontal(classes="role-row-fields"):
            yield Select(
                [(cli, cli) for cli in KNOWN_CLIS],
                id=f"{self.role}-cli",
                allow_blank=False,
                classes="cli-select",
            )
            with Vertical(classes="model-column"):
                yield Input(
                    placeholder=MODEL_FILTER_PROMPT,
                    id=f"{self.role}-model-filter",
                    classes="model-filter",
                )
                yield Select(
                    [],
                    id=f"{self.role}-model",
                    prompt=MODEL_PROMPT,
                    allow_blank=True,
                    classes="model-select",
                )
            yield Select(
                [],
                id=f"{self.role}-effort",
                prompt=EFFORT_PROMPT,
                allow_blank=True,
                classes="effort-select",
            )


class RolesForm(Static):
    """One ``RoleRow`` per pipeline role + option refresh logic."""

    def __init__(self):
        super().__init__()
        self.models: dict[str, list[str]] = {}
        self.variants: dict[str, dict[str, list[str]]] = {}

    def compose(self) -> ComposeResult:
        for role, title_key in ROLES:
            yield RoleRow(role, title_key)

    # -------------------------------------------------------------- #
    # Values
    # -------------------------------------------------------------- #
    def set_role(self, role: str, cli: str, model: str, effort: str = "") -> None:
        """Set the CLI, model and effort of a role (e.g. when loading)."""
        self.query_one(f"#{role}-cli", Select).value = cli
        self._set_model_value(role, model)
        self._set_effort_value(role, effort)

    def role_values(self, role: str) -> tuple[str, str, str]:
        """Return ``(cli, model, effort)``; empty values mean default."""
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        model_value = self.query_one(f"#{role}-model", Select).value
        model = "" if model_value is Select.NULL else str(model_value)
        effort_value = self.query_one(f"#{role}-effort", Select).value
        effort = "" if effort_value is Select.NULL else str(effort_value)
        return cli, model, effort

    # -------------------------------------------------------------- #
    # Model options
    # -------------------------------------------------------------- #
    def _set_model_value(self, role: str, model: str) -> None:
        select = self.query_one(f"#{role}-model", Select)
        select.set_options([(model, model)] if model else [])
        select.value = model if model else Select.NULL

    def _set_effort_value(self, role: str, effort: str) -> None:
        select = self.query_one(f"#{role}-effort", Select)
        select.set_options([(effort, effort)] if effort else [])
        select.value = effort if effort else Select.NULL

    def set_models(self, models_map: dict[str, list[str]]) -> None:
        """Apply the already-loaded model catalogue (repopulates the selects)."""
        self.models = models_map
        for role, _ in ROLES:
            self._refresh_model_options(role)

    def set_variants(self, variants_map: dict[str, dict[str, list[str]]]) -> None:
        """Apply the already-loaded per-CLI+model variants catalogue."""
        self.variants = variants_map
        for role, _ in ROLES:
            self._refresh_effort_options(role)

    def _refresh_model_options(self, role: str) -> None:
        """Repopulate model options preserving the selection when applicable.

        Rules: if the CLI has not loaded models yet, whatever is there is
        kept (e.g. the saved value). If there is already a list and the
        selection does not belong to the current CLI (CLI change), it is
        reset to ``default``. The filter input text (``#{role}-model-filter``)
        narrows the visible options, but the chosen model is always
        preserved even if it does not match.
        """
        select = self.query_one(f"#{role}-model", Select)
        current = select.value
        chosen = "" if current is Select.NULL else str(current)
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        models = list(self.models.get(cli, []))
        if chosen and models and chosen not in models:
            chosen = ""  # CLI changed: the previous model no longer applies
        if chosen and chosen not in models:
            models.append(chosen)
        query = self.query_one(f"#{role}-model-filter", Input).value
        options = filter_models(models, query)
        if chosen and chosen not in options:
            options.append(chosen)  # the selection is never hidden
        select.set_options([(model, model) for model in options])
        select.value = chosen if chosen else Select.NULL

    def _refresh_effort_options(self, role: str) -> None:
        """Repopulate effort options from ``self.variants``.

        Same rule as ``_refresh_model_options``: if the level list is not
        loaded yet, the saved value is preserved. If there is already a
        list and the saved value does not belong to it, it is reset to
        empty only when there is a loaded catalogue for that CLI; in any
        other case the value is added to the options so it is not lost.
        """
        select = self.query_one(f"#{role}-effort", Select)
        current = select.value
        chosen = "" if current is Select.NULL else str(current)
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        model_value = self.query_one(f"#{role}-model", Select).value
        model = "" if model_value is Select.NULL else str(model_value)
        levels = list(self.variants.get(cli, {}).get(model, []))
        if chosen and levels and chosen not in levels:
            chosen = ""
        if chosen and chosen not in levels:
            levels.append(chosen)
        select.set_options([(level, level) for level in levels])
        select.value = chosen if chosen else Select.NULL

    def on_select_changed(self, event: Select.Changed) -> None:
        for role, _ in ROLES:
            if event.select.id == f"{role}-cli":
                self.query_one(f"#{role}-model-filter", Input).value = ""
                self._refresh_model_options(role)
                self._refresh_effort_options(role)
            elif event.select.id == f"{role}-model":
                self._refresh_effort_options(role)

    def on_input_changed(self, event: Input.Changed) -> None:
        for role, _ in ROLES:
            if event.input.id == f"{role}-model-filter":
                self._refresh_model_options(role)
