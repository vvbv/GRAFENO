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


def test_used_keys_are_defined_in_catalog():
    """Todas las claves ``t("...")`` usadas en ``src/`` están en el catálogo.

    Recorre los ``.py`` bajo ``src/grafeno/`` y extrae los primeros argumentos
    literales de ``t(...)`` para detectar claves que el código usa pero que
    faltan en ``_MESSAGES``. Evita regresiones del estilo "se añade la clave
    en código pero se olvida su traducción".
    """
    import re
    from pathlib import Path

    src_root = Path("src")
    pattern = re.compile(r"""(?<![\w.])t\(\s*["']([a-zA-Z][\w.]*)["']""")
    used: set[str] = set()
    for path in src_root.rglob("*.py"):
        used.update(pattern.findall(path.read_text(encoding="utf-8")))

    defined_en = set(i18n._MESSAGES["en"])
    defined_es = set(i18n._MESSAGES["es"])
    missing_en = sorted(used - defined_en)
    missing_es = sorted(used - defined_es)
    assert not missing_en, f"Claves usadas en src y NO definidas en en: {missing_en}"
    assert not missing_es, f"Claves usadas en src y NO definidas en es: {missing_es}"


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
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            select = app.screen.query_one("#cfg-language", Select)
            assert str(select.value) == "en"
            select.value = "es"
            await pilot.pause()
            app.screen.query_one("#cfg-save").scroll_visible()
            # Deja tiempo a que el scroll deje el botón en el viewport visible.
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        assert config_module.load().language == "es"
        assert i18n.current_language() == "es"

    asyncio.run(scenario())
