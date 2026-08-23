"""Tests de la pantalla de configuración (modelos mockeados, sin CLIs reales)."""

from __future__ import annotations

import asyncio

from grafeno import config as config_module
from grafeno.app import GrafenoApp
from grafeno.drivers.base import CLIDriver
from grafeno.tui.screens.config import ConfigScreen
from grafeno.tui.screens.tasks import TaskListScreen
from textual.widgets import Select


class _FakeDriver(CLIDriver):
    def __init__(self, name: str, models: list[str]):
        self.name = name
        self.display_name = name
        self.executable = name
        self._models = models

    def build_command(self, request):
        return []

    def list_models(self):
        return list(self._models)


_FAKES = {
    "opencode": _FakeDriver("opencode", ["opencode-go/kimi-k3", "opencode/big-pickle"]),
    "kimi": _FakeDriver("kimi", ["kimi-code/k3"]),
}


def test_config_screen_loads_models_and_saves(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.get_driver", _FAKES.get)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Espera a que el worker en hilo cargue los modelos.
            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen._models:
                    break
            assert set(app.screen._models) == {"opencode", "kimi"}

            # El planificador (opencode) tiene sus modelos desplegables.
            planner_model = app.screen.query_one("#planner-model", Select)
            assert planner_model.value is Select.NULL  # sin modelo guardado
            planner_model.value = "opencode-go/kimi-k3"

            # Cambiar el CLI del implementador a kimi repuebla sus opciones.
            impl_cli = app.screen.query_one("#implementer-cli", Select)
            impl_cli.value = "kimi"
            await pilot.pause()
            impl_model = app.screen.query_one("#implementer-model", Select)
            impl_model.value = "kimi-code/k3"

            app.screen.query_one("#cfg-save").scroll_visible()
            await pilot.pause()
            await pilot.click("#cfg-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

        saved = config_module.load()
        assert saved.planner.model == "opencode-go/kimi-k3"
        assert saved.implementer.cli == "kimi"
        assert saved.implementer.model == "kimi-code/k3"
        assert saved.reviewer.model == ""

    asyncio.run(scenario())


def test_config_screen_escape_goes_back(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.get_driver", _FAKES.get)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_saved_model_survives_mount(monkeypatch):
    """Regresión: al montar, el evento Changed del Select de CLI no debe
    borrar el modelo guardado en config.toml."""
    from grafeno.config import Config

    cfg = Config()
    cfg.planner.model = "opencode-go/kimi-k3"
    cfg.implementer.cli = "kimi"
    cfg.implementer.model = "kimi-code/k3"
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.config.get_driver", _FAKES.get)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            # Deja procesar los Changed asíncronos del montaje y la carga.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen._models:
                    break
            await pilot.pause()

            planner = app.screen.query_one("#planner-model", Select)
            implementer = app.screen.query_one("#implementer-model", Select)
            assert str(planner.value) == "opencode-go/kimi-k3"
            assert str(implementer.value) == "kimi-code/k3"

    asyncio.run(scenario())
