"""Pantalla de configuración global (planner / implementer / reviewer / automode)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from ... import config as config_module
from ...config import KNOWN_CLIS, Config
from ...drivers import get_driver

_ROLES = (
    ("planner", "Planificador"),
    ("implementer", "Implementador"),
    ("reviewer", "Revisor"),
)

_MODEL_PROMPT = "Modelo por defecto del CLI"


class RoleRow(Static):
    """Fila de configuración de un rol: Select de CLI + Select de modelo."""

    def __init__(self, role: str, title: str):
        super().__init__(classes="role-row")
        self.role = role
        self._title = title

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="role-title")
        yield Select(
            [(cli, cli) for cli in KNOWN_CLIS],
            id=f"{self.role}-cli",
            allow_blank=False,
            classes="cli-select",
        )
        yield Select(
            [],
            id=f"{self.role}-model",
            prompt=_MODEL_PROMPT,
            allow_blank=True,
            classes="model-select",
        )


class ConfigScreen(Screen[None]):
    BINDINGS = [Binding("escape", "back", "Volver")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="config-container"):
            yield Label("Configuración global (~/.grafeno/config.toml)", id="config-title")
            yield Static("Roles del pipeline", classes="section-title")
            for role, title in _ROLES:
                yield RoleRow(role, title)
            yield Static("Cargando modelos de los CLIs…", id="models-status")
            yield Static("Automode (valores por defecto para nuevas tareas)", classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Checkbox("Automode activado", id="am-enabled")
                yield Checkbox("Crear rama git por tarea", id="am-branch")
            with Horizontal(classes="automode-row"):
                yield Checkbox("Automode: preguntar si el plan está bien antes de implementar", id="am-confirm-plan")
            with Horizontal(classes="automode-row"):
                yield Label("Iteraciones máx.")
                yield Input(id="am-max-iter", type="integer", placeholder="5")
                yield Label("Comando de tests")
                yield Input(id="am-tests", placeholder="p. ej. pytest -q")
            with Horizontal(id="config-buttons"):
                yield Button("Guardar", variant="primary", id="cfg-save")
                yield Button("Volver", id="cfg-back")
        yield Footer()

    def on_mount(self) -> None:
        self._config = config_module.load()
        self._models: dict[str, list[str]] = {}
        for role, _ in _ROLES:
            role_cfg = self._config.role(role)
            self.query_one(f"#{role}-cli", Select).value = role_cfg.cli
            # Muestra el valor guardado de inmediato; las opciones llegan async.
            self._set_model_value(role, role_cfg.model)
        auto = self._config.automode
        self.query_one("#am-enabled", Checkbox).value = auto.enabled
        self.query_one("#am-branch", Checkbox).value = auto.create_branch
        self.query_one("#am-confirm-plan", Checkbox).value = auto.confirm_plan
        self.query_one("#am-max-iter", Input).value = str(auto.max_iterations)
        self.query_one("#am-tests", Input).value = auto.test_command
        self._load_models()

    def _set_model_value(self, role: str, model: str) -> None:
        select = self.query_one(f"#{role}-model", Select)
        select.set_options([(model, model)] if model else [])
        select.value = model if model else Select.NULL

    # ------------------------------------------------------------------ #
    # Carga de modelos (en hilo: no bloquea la UI)
    # ------------------------------------------------------------------ #
    def _load_models(self) -> None:
        self.run_worker(
            self._fetch_models,
            thread=True,
            exclusive=True,
            group="models",
            exit_on_error=False,
        )

    def _fetch_models(self) -> None:
        models_map: dict[str, list[str]] = {}
        for cli in KNOWN_CLIS:
            try:
                models_map[cli] = get_driver(cli).list_models()
            except Exception:
                models_map[cli] = []
        self.app.call_from_thread(self._apply_models, models_map)

    def _apply_models(self, models_map: dict[str, list[str]]) -> None:
        self._models = models_map
        for role, _ in _ROLES:
            self._refresh_model_options(role)
        summary = " · ".join(
            f"{cli}: {len(models)} modelos" if models else f"{cli}: no disponible"
            for cli, models in models_map.items()
        )
        self.query_one("#models-status", Static).update(summary)

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
        models = list(self._models.get(cli, []))
        if chosen and models and chosen not in models:
            chosen = ""  # el CLI cambió: el modelo anterior no aplica
        if chosen and chosen not in models:
            models.append(chosen)
        select.set_options([(model, model) for model in models])
        select.value = chosen if chosen else Select.NULL

    # ------------------------------------------------------------------ #
    def on_select_changed(self, event: Select.Changed) -> None:
        for role, _ in _ROLES:
            if event.select.id == f"{role}-cli":
                self._refresh_model_options(role)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-back":
            self.dismiss()
            return
        self._save()

    def action_back(self) -> None:
        self.dismiss()

    def _save(self) -> None:
        try:
            max_iter = int(self.query_one("#am-max-iter", Input).value or "5")
        except ValueError:
            self.notify("Iteraciones máximas debe ser un entero.", severity="error")
            return
        if max_iter < 1:
            self.notify("Iteraciones máximas debe ser ≥ 1.", severity="error")
            return

        cfg = Config()
        for role, _ in _ROLES:
            role_cfg = cfg.role(role)
            role_cfg.cli = str(self.query_one(f"#{role}-cli", Select).value)
            value = self.query_one(f"#{role}-model", Select).value
            role_cfg.model = "" if value is Select.NULL else str(value)
        cfg.automode.enabled = self.query_one("#am-enabled", Checkbox).value
        cfg.automode.create_branch = self.query_one("#am-branch", Checkbox).value
        cfg.automode.confirm_plan = self.query_one("#am-confirm-plan", Checkbox).value
        cfg.automode.max_iterations = max_iter
        cfg.automode.test_command = self.query_one("#am-tests", Input).value.strip()
        config_module.save(cfg)
        self.notify("Configuración guardada.")
        self.dismiss()
