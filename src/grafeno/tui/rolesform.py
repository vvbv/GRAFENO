"""Formulario reutilizable de roles del pipeline (CLI + modelo por rol).

Lo usan la pantalla de configuración global y el modal de configuración
por tarea. La carga de modelos la hace el contenedor (pantalla), que llama
a ``set_models`` cuando ``grafeno.drivers.fetch_all_models`` termina.
"""

from __future__ import annotations

from textual.app import ComposeResult
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


class RoleRow(Static):
    """Fila de configuración de un rol: Select de CLI + Select de modelo."""

    def __init__(self, role: str, title_key: str):
        super().__init__(classes="role-row")
        self.role = role
        self._title_key = title_key

    def compose(self) -> ComposeResult:
        yield Label(t(self._title_key), classes="role-title")
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


class RolesForm(Static):
    """Una ``RoleRow`` por rol del pipeline + lógica de refresco de opciones de modelo."""

    def __init__(self):
        super().__init__()
        self.models: dict[str, list[str]] = {}

    def compose(self) -> ComposeResult:
        for role, title_key in ROLES:
            yield RoleRow(role, title_key)

    # -------------------------------------------------------------- #
    # Valores
    # -------------------------------------------------------------- #
    def set_role(self, role: str, cli: str, model: str) -> None:
        """Fija el CLI y el modelo de un rol (p.ej. al cargar la config)."""
        self.query_one(f"#{role}-cli", Select).value = cli
        self._set_model_value(role, model)

    def role_values(self, role: str) -> tuple[str, str]:
        """Devuelve (cli, modelo) seleccionados; modelo vacío = default."""
        cli = str(self.query_one(f"#{role}-cli", Select).value)
        value = self.query_one(f"#{role}-model", Select).value
        model = "" if value is Select.NULL else str(value)
        return cli, model

    # -------------------------------------------------------------- #
    # Opciones de modelo
    # -------------------------------------------------------------- #
    def _set_model_value(self, role: str, model: str) -> None:
        select = self.query_one(f"#{role}-model", Select)
        select.set_options([(model, model)] if model else [])
        select.value = model if model else Select.NULL

    def set_models(self, models_map: dict[str, list[str]]) -> None:
        """Aplica el catálogo de modelos ya cargado (repuebla los selects)."""
        self.models = models_map
        for role, _ in ROLES:
            self._refresh_model_options(role)

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

    def on_select_changed(self, event: Select.Changed) -> None:
        for role, _ in ROLES:
            if event.select.id == f"{role}-cli":
                self._refresh_model_options(role)
