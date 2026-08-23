"""Tests del catálogo i18n y del selector de idioma."""

from __future__ import annotations

import asyncio

from grafeno import config as config_module, i18n
from grafeno.i18n import t


def test_t_defaults_to_english():
    i18n.set_language("en")
    assert t("common.save") == "Save"


def test_t_spanish():
    i18n.set_language("es")
    assert t("common.save") == "Guardar"


def test_t_kwargs_interpolation():
    i18n.set_language("en")
    assert t("orch.max_iterations", max=3) == "Maximum iterations reached (3). Check the review files."


def test_t_unknown_key_falls_back_to_key():
    assert t("no.existe.esta.clave") == "no.existe.esta.clave"


def test_t_invalid_language_falls_back_to_default():
    i18n.set_language("fr")
    assert i18n.current_language() == "en"
    assert t("common.save") == "Save"


def test_catalog_parity():
    """Ambos idiomas definen exactamente las mismas claves."""
    assert set(i18n._MESSAGES["en"]) == set(i18n._MESSAGES["es"])


def test_config_language_roundtrip():
    cfg = config_module.load()
    assert cfg.language == "en"  # defecto
    cfg.language = "es"
    config_module.save(cfg)
    assert config_module.load().language == "es"


def test_config_screen_language_select_persists():
    from grafeno.config import Config
    config_module.save(Config())

    async def scenario():
        from grafeno.app import GrafenoApp
        from grafeno.tui.screens.config import ConfigScreen
        from textual.widgets import Select

        app = GrafenoApp()
        async with app.run_test(size=(110, 60)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            select = app.screen.query_one("#cfg-language", Select)
            assert str(select.value) == "en"
            select.value = "es"
            await pilot.pause()
            app.screen.query_one("#cfg-save").scroll_visible()
            await pilot.pause()
            await pilot.click("#cfg-save")
            await pilot.pause()

        assert config_module.load().language == "es"
        assert i18n.current_language() == "es"

    asyncio.run(scenario())
