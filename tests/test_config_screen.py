"""Tests de la pantalla de configuración (modelos mockeados, sin CLIs reales)."""

from __future__ import annotations

import asyncio

from grafeno import config as config_module
from grafeno.app import GrafenoApp
from grafeno.tui.rolesform import RolesForm
from grafeno.tui.screens.config import ConfigScreen
from grafeno.tui.screens.tasks import TaskListScreen
from textual.widgets import Select, Static, TextArea


async def _fake_fetch(clis):
    return {
        "opencode": ["opencode-go/kimi-k3", "opencode/big-pickle"],
        "kimi": ["kimi-code/k3"],
    }


def test_config_screen_loads_models_and_saves(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Espera a que el worker asíncrono cargue los modelos.
            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            assert set(app.screen.query_one(RolesForm).models) == {"opencode", "kimi"}

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

            # La fila del rol final existe con su CLI por defecto.
            final_cli = app.screen.query_one("#final-cli", Select)
            assert str(final_cli.value) == "opencode"

            app.screen.query_one("#cfg-save").scroll_visible()
            # Da tiempo a que el scroll asiente antes de pulsar.
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

        saved = config_module.load()
        assert saved.planner.model == "opencode-go/kimi-k3"
        assert saved.implementer.cli == "kimi"
        assert saved.implementer.model == "kimi-code/k3"
        assert saved.reviewer.model == ""
        assert saved.final.cli == "opencode"
        assert saved.final.model == ""

    asyncio.run(scenario())


def test_config_screen_escape_cancels_loading(monkeypatch):
    """El primer Esc cancela la carga de modelos; el segundo cierra."""
    from grafeno import i18n

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_fetch(clis):
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _slow_fetch)

    async def scenario():
        # El texto del estado se muestra en el idioma activo; lo cambiamos a
        # español para que la aserción sea estable independiente del idioma.
        i18n.set_language("es")
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            await asyncio.wait_for(started.wait(), timeout=2)

            await pilot.press("escape")  # cancela la carga, NO cierra
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            status = app.screen.query_one("#models-status", Static).render()
            status_text = status.plain if hasattr(status, "plain") else str(status)
            assert "cancelada" in status_text
            release.set()

            await pilot.press("escape")  # ahora sí cierra
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_config_screen_escape_goes_back(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            await pilot.pause()
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
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            # Deja procesar los Changed asíncronos del montaje y la carga.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            planner = app.screen.query_one("#planner-model", Select)
            implementer = app.screen.query_one("#implementer-model", Select)
            assert str(planner.value) == "opencode-go/kimi-k3"
            assert str(implementer.value) == "kimi-code/k3"

    asyncio.run(scenario())


def test_config_screen_final_prompt_persists(monkeypatch):
    """El TextArea de pasos finales carga el valor global y lo guarda en config."""
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Espera a que el worker de modelos termine antes de tocar el save.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            area = app.screen.query_one("#cfg-final-prompt", TextArea)
            assert area.text == ""  # sin valor guardado por defecto

            area.text = "Revisa el CHANGELOG\ny actualiza README"
            app.screen.query_one("#cfg-save").scroll_visible()
            # Deja tiempo a que la posición se actualice tras el scroll.
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        saved = config_module.load()
        assert saved.final_prompt == "Revisa el CHANGELOG\ny actualiza README"

    asyncio.run(scenario())


def test_config_screen_final_prompt_loads_existing(monkeypatch):
    """Si la config ya define final_prompt, el TextArea lo muestra al montar."""
    from grafeno.config import Config

    cfg = Config()
    cfg.final_prompt = "valor previo"
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            area = app.screen.query_one("#cfg-final-prompt", TextArea)
            assert area.text == "valor previo"

    asyncio.run(scenario())


def test_config_screen_hook_persists(monkeypatch):
    """El Input de hook y los checkboxes de etapas guardan y restauran valores."""
    from textual.widgets import Checkbox

    from grafeno.pipeline.hooks import HOOK_STAGES

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Espera a que el worker de modelos termine antes de tocar el save.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            app.screen.query_one("#hook-command").value = "./notify.sh"
            app.screen.query_one("#hook-stage-plan", Checkbox).value = True
            app.screen.query_one("#hook-stage-final", Checkbox).value = True

            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        saved = config_module.load()
        assert saved.hook.command == "./notify.sh"
        for stage in HOOK_STAGES:
            expected = stage in ("plan", "final")
            actual = stage in saved.hook.stages.split(",") if saved.hook.stages else False
            assert actual is expected

    asyncio.run(scenario())


def test_editor_section_roundtrip(monkeypatch):
    """La sección de editor persiste enabled/editor/mode/side en config.toml."""
    from textual.widgets import Checkbox

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr(
        "grafeno.tui.screens.config.editor_module.available_editors",
        lambda: ["zed", "tode"],
    )

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Espera a que el worker de modelos termine antes de tocar el save.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            app.screen.query_one("#editor-enabled", Checkbox).value = True
            editor_select = app.screen.query_one("#editor-name", Select)
            editor_select.value = "tode"
            app.screen.query_one("#editor-mode", Select).value = "split"
            app.screen.query_one("#editor-side", Select).value = "right"
            await pilot.pause()

            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        saved = config_module.load()
        assert saved.editor.enabled is True
        assert saved.editor.editor == "tode"
        assert saved.editor.mode == "split"
        assert saved.editor.side == "right"

    asyncio.run(scenario())
