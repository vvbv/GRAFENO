"""Pantalla de configuración global (planner / implementer / reviewer / automode)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.suggester import SuggestFromList
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


class RoleRow(Static):
    """Fila de configuración de un rol: Select de CLI + Input de modelo."""

    def __init__(self, role: str, title: str):
        super().__init__(classes="role-row")
        self.role = role
        self._title = title

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="role-title")
        yield Select([(cli, cli) for cli in KNOWN_CLIS], id=f"{self.role}-cli", allow_blank=False)
        yield Input(placeholder="modelo (vacío = default del CLI)", id=f"{self.role}-model")

    @property
    def cli_select(self) -> Select:
        return self.query_one(f"#{self.role}-cli", Select)

    @property
    def model_input(self) -> Input:
        return self.query_one(f"#{self.role}-model", Input)


class ConfigScreen(Screen[None]):
    BINDINGS = [Binding("escape", "back", "Volver")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="config-container"):
            yield Label("Configuración global (~/.grafeno/config.toml)", id="config-title")
            yield Static("Roles del pipeline", classes="section-title")
            for role, title in _ROLES:
                yield RoleRow(role, title)
            yield Static("Automode (valores por defecto para nuevas tareas)", classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Checkbox("Automode activado", id="am-enabled")
                yield Checkbox("Crear rama git por tarea", id="am-branch")
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
        for role, _ in _ROLES:
            row = self.query_one(f".role-row #{role}-cli", Select)
            row.value = self._config.role(role).cli
            self.query_one(f"#{role}-model", Input).value = self._config.role(role).model
        auto = self._config.automode
        self.query_one("#am-enabled", Checkbox).value = auto.enabled
        self.query_one("#am-branch", Checkbox).value = auto.create_branch
        self.query_one("#am-max-iter", Input).value = str(auto.max_iterations)
        self.query_one("#am-tests", Input).value = auto.test_command
        self._load_models()

    def _load_models(self) -> None:
        """Carga los modelos de cada CLI en segundo plano para el autocompletado."""
        self._models: dict[str, list[str]] = {}

        async def load() -> None:
            for cli in KNOWN_CLIS:
                try:
                    self._models[cli] = get_driver(cli).list_models()
                except Exception:
                    self._models[cli] = []
            self._apply_suggesters()

        self.run_worker(load(), exclusive=True, thread=False, group="models")

    def _apply_suggesters(self) -> None:
        for role, _ in _ROLES:
            row_select = self.query_one(f"#{role}-cli", Select)
            cli = str(row_select.value)
            self.query_one(f"#{role}-model", Input).suggester = SuggestFromList(
                self._models.get(cli, []), case_sensitive=False
            )

    def on_select_changed(self, event: Select.Changed) -> None:
        for role, _ in _ROLES:
            if event.select.id == f"{role}-cli":
                self.query_one(f"#{role}-model", Input).suggester = SuggestFromList(
                    self._models.get(str(event.value), []), case_sensitive=False
                )

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
            role_cfg.model = self.query_one(f"#{role}-model", Input).value.strip()
        cfg.automode.enabled = self.query_one("#am-enabled", Checkbox).value
        cfg.automode.create_branch = self.query_one("#am-branch", Checkbox).value
        cfg.automode.max_iterations = max_iter
        cfg.automode.test_command = self.query_one("#am-tests", Input).value.strip()
        config_module.save(cfg)
        self.notify("Configuración guardada.")
        self.dismiss()
