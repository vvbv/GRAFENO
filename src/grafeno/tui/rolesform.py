"""Formulario reutilizable de roles del pipeline (CLI + modelo + esfuerzo por rol).

Lo usan la pantalla de configuración global y el modal de configuración
por tarea. La carga de modelos y variantes la hace el contenedor (pantalla),
que llama a ``set_models`` y ``set_variants`` cuando ``fetch_all_models`` y
``fetch_all_variants`` terminan.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Label, Select, Static

from ..config import KNOWN_CLIS
from ..i18n import t

# Roles del pipeline (id, clave de i18n para el título).
ROLES: tuple[tuple[str, str], ...] = (
    ("planner", "cfg.role.planner"),
    ("implementer", "cfg.role.implementer"),
    ("reviewer", "cfg.role.reviewer"),
    ("final", "cfg.role.final"),
)

MODEL_PROMPT = t("cfg.model.prompt")
EFFORT_PROMPT = t("cfg.effort.prompt")


class RoleRow(Static):
    """Fila de configuración de un rol: Select de CLI + modelo + esfuerzo."""

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
    """Una ``RoleRow`` por rol del pipeline + lógica de refresco de opciones."""

    def __init__(self):
        super().__init__()
        self.models: dict[str, list[str]] = {}
        self.variants: dict[str, dict[str, list[str]]] = {}

    def compose(self) -> ComposeResult:
        for role, title_key in ROLES:
            yield RoleRow(role, title_key)

    # -------------------------------------------------------------- #
    # Valores
    # -------------------------------------------------------------- #
    def set_role(self, role: str, cli: str, model: str, effort: str = "") -> None:
        """Fija el CLI, el modelo y el esfuerzo de un rol (p.ej. al cargar)."""
        self.query_one(f"#{role}-cli", Select).value = cli
        self._set_model_value(role, model)
        self._set_effort_value(role, effort)

    def role_values(self, role: str) -> tuple[str, str, str]:
        """Devuelve ``(cli, modelo, esfuerzo)``; vacíos = default."""
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        model_value = self.query_one(f"#{role}-model", Select).value
        model = "" if model_value is Select.NULL else str(model_value)
        effort_value = self.query_one(f"#{role}-effort", Select).value
        effort = "" if effort_value is Select.NULL else str(effort_value)
        return cli, model, effort

    # -------------------------------------------------------------- #
    # Opciones de modelo
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
        """Aplica el catálogo de modelos ya cargado (repuebla los selects)."""
        self.models = models_map
        for role, _ in ROLES:
            self._refresh_model_options(role)

    def set_variants(self, variants_map: dict[str, dict[str, list[str]]]) -> None:
        """Aplica el catálogo de variantes por CLI+modelo ya cargado."""
        self.variants = variants_map
        for role, _ in ROLES:
            self._refresh_effort_options(role)

    def _refresh_model_options(self, role: str) -> None:
        """Repuebla las opciones del modelo conservando la selección si aplica.

        Reglas: si el CLI aún no ha cargado modelos, se conserva lo que haya
        (p.ej. el valor guardado). Si ya hay lista y la selección no pertenece
        al CLI actual (cambio de CLI), se reinicia a "default".
        """
        select = self.query_one(f"#{role}-model", Select)
        current = select.value
        chosen = "" if current is Select.NULL else str(current)
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        models = list(self.models.get(cli, []))
        if chosen and models and chosen not in models:
            chosen = ""  # el CLI cambió: el modelo anterior no aplica
        if chosen and chosen not in models:
            models.append(chosen)
        select.set_options([(model, model) for model in models])
        select.value = chosen if chosen else Select.NULL

    def _refresh_effort_options(self, role: str) -> None:
        """Repuebla las opciones de esfuerzo a partir de ``self.variants``.

        Misma regla que ``_refresh_model_options``: si no hay lista de
        niveles cargada aún, se conserva el valor guardado. Si ya hay lista
        y el valor guardado no pertenece a ella, se reinicia a vacío solo
        cuando hay catálogo cargado para ese CLI; en cualquier otro caso
        se añade el valor a las opciones para no perderlo.
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
                self._refresh_model_options(role)
                self._refresh_effort_options(role)
            elif event.select.id == f"{role}-model":
                self._refresh_effort_options(role)
