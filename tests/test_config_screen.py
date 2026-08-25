"""Tests of the settings screen (mocked models, no real CLIs)."""

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


async def _fake_fetch_variants(clis):
    return {}


def test_config_screen_loads_models_and_saves(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Wait for the async worker to load the models.
            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            assert set(app.screen.query_one(RolesForm).models) == {"opencode", "kimi"}

            # The planner (opencode) has its models expanded.
            planner_model = app.screen.query_one("#planner-model", Select)
            assert planner_model.value is Select.NULL  # no saved model
            planner_model.value = "opencode-go/kimi-k3"

            # Changing the implementer's CLI to kimi repopulates its options.
            impl_cli = app.screen.query_one("#implementer-cli", Select)
            impl_cli.value = "kimi"
            await pilot.pause()
            impl_model = app.screen.query_one("#implementer-model", Select)
            impl_model.value = "kimi-code/k3"

            # The final role row exists with its default CLI.
            final_cli = app.screen.query_one("#final-cli", Select)
            assert str(final_cli.value) == "opencode"

            app.screen.query_one("#cfg-save").scroll_visible()
            # Give the scroll time to settle before clicking.
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
    """The first Esc cancels model loading; the second one closes."""
    from grafeno import i18n

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_fetch(clis):
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _slow_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _slow_fetch)

    async def scenario():
        # The status text is shown in the active language; we switch to
        # Spanish so the assertion is stable regardless of language.
        i18n.set_language("es")
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            await asyncio.wait_for(started.wait(), timeout=2)

            await pilot.press("escape")  # cancels the load, does NOT close
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)
            status = app.screen.query_one("#models-status", Static).render()
            status_text = status.plain if hasattr(status, "plain") else str(status)
            assert "cancelada" in status_text
            release.set()

            await pilot.press("escape")  # now closes
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_config_screen_escape_goes_back(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

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
    """Regression: on mount, the Select of CLI's Changed event must not
    clear the model saved in config.toml."""
    from grafeno.config import Config

    cfg = Config()
    cfg.planner.model = "opencode-go/kimi-k3"
    cfg.implementer.cli = "kimi"
    cfg.implementer.model = "kimi-code/k3"
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            # Let the async Changed events of mount and load settle.
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
    """The final steps TextArea loads the global value and saves it to config."""
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Wait for the models worker to finish before touching save.
            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            area = app.screen.query_one("#cfg-final-prompt", TextArea)
            assert area.text == ""  # no saved value by default

            area.text = "Revisa el CHANGELOG\ny actualiza README"
            app.screen.query_one("#cfg-save").scroll_visible()
            # Give time for the position to update after scrolling.
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        saved = config_module.load()
        assert saved.final_prompt == "Revisa el CHANGELOG\ny actualiza README"

    asyncio.run(scenario())


def test_config_screen_final_prompt_loads_existing(monkeypatch):
    """If config already defines final_prompt, the TextArea shows it on mount."""
    from grafeno.config import Config

    cfg = Config()
    cfg.final_prompt = "valor previo"
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

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
    """The hook Input and stage checkboxes save and restore values."""
    from textual.widgets import Checkbox

    from grafeno.pipeline.hooks import HOOK_STAGES

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            # Wait for the models worker to finish before touching save.
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
    """The editor section persists enabled/editor/mode/side in config.toml."""
    from textual.widgets import Checkbox

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)
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

            # Wait for the models worker to finish before touching save.
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


def test_config_screen_effort_select_offers_variants(monkeypatch):
    """When CLI+model with variants is set, the effort select offers them."""
    async def _fake_variants(clis):
        return {"opencode": {"opencode-go/kimi-k3": ["low", "high"]}}

    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).variants:
                    break

            planner_model = app.screen.query_one("#planner-model", Select)
            planner_model.value = "opencode-go/kimi-k3"
            await pilot.pause()

            planner_effort = app.screen.query_one("#planner-effort", Select)
            options = [str(value) for value, _ in planner_effort._options]  # noqa: SLF001
            assert "low" in options
            assert "high" in options

    asyncio.run(scenario())


def test_config_screen_saves_effort_per_role(monkeypatch):
    """The effort selected in the form persists in config.toml."""
    from grafeno.config import Config

    cfg = Config()
    config_module.save(cfg)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.config.fetch_all_variants", _fake_fetch_variants)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ConfigScreen)

            for _ in range(20):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            form = app.screen.query_one(RolesForm)
            form.set_role("planner", "opencode", "opencode-go/kimi-k3", "high")
            await pilot.pause()

            app.screen.query_one("#cfg-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#cfg-save")
            await pilot.pause()

        saved = config_module.load()
        assert saved.planner.effort == "high"

    asyncio.run(scenario())
