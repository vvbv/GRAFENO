"""Tests of the per-task agents modal (mocked models, no real CLIs)."""

from __future__ import annotations

import asyncio

from grafeno import models
from grafeno.app import GrafenoApp
from grafeno.tui.rolesform import RolesForm
from grafeno.tui.screens.detail import TaskDetailScreen
from grafeno.tui.screens.roles import TaskRolesScreen
from textual.widgets import Select, Static


async def _fake_fetch(clis):
    return {
        "opencode": ["opencode-go/kimi-k3", "opencode/big-pickle"],
        "kimi": ["kimi-code/k3"],
    }


async def _fake_fetch_variants(clis):
    return {}


def _make_task():
    from grafeno.config import Config

    task = models.Task.create(
        name="Tarea de prueba",
        description="",
        workdir="/tmp",
        config=Config(),
    )
    models.save(task)
    return task


def test_task_roles_screen_edits_and_saves(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            # Wait for the model catalogue to arrive.
            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break

            # Switch the planner to kimi + a concrete model.
            app.screen.query_one("#planner-cli", Select).value = "kimi"
            await pilot.pause()
            app.screen.query_one("#planner-model", Select).value = "kimi-code/k3"

            app.screen.query_one("#tr-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.planner.cli == "kimi"
    assert reloaded.planner.model == "kimi-code/k3"
    # The remaining roles are not touched.
    assert reloaded.implementer.cli == task.implementer.cli
    assert reloaded.reviewer.model == task.reviewer.model


def test_task_roles_screen_cancel_keeps_roles(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()
    original_cli = task.planner.cli

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            app.screen.query_one("#tr-cancel").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#tr-cancel")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())
    assert models.load(task.id).planner.cli == original_cli


def test_task_roles_screen_escape_cancels_loading(monkeypatch):
    from grafeno import i18n

    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_fetch(clis):
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _slow_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _slow_fetch)
    task = _make_task()

    async def scenario():
        # Force Spanish so the text assertion is stable.
        i18n.set_language("es")
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)
            await asyncio.wait_for(started.wait(), timeout=2)

            await pilot.press("escape")  # cancels the load, does NOT close
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)
            status = app.screen.query_one("#roles-status", Static).render()
            status_text = status.plain if hasattr(status, "plain") else str(status)
            assert "cancelada" in status_text
            release.set()

            await pilot.press("escape")  # now closes without saving
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())


def test_task_roles_screen_persists_effort(monkeypatch):
    """The effort selected in the modal is saved into task.toml."""
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break
            await pilot.pause()

            form = app.screen.query_one(RolesForm)
            form.set_role("planner", "opencode", "opencode-go/kimi-k3", "low")
            await pilot.pause()

            app.screen.query_one("#tr-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.planner.effort == "low"


def test_task_roles_screen_profile_applies_roles_and_persists(monkeypatch):
    """Choosing a profile in the modal fills the form and persists the name."""
    from grafeno import profiles as profiles_module
    from grafeno.config import RoleConfig
    from grafeno.profiles import Profile

    profile = Profile(name="calidad")
    profile.implementer = RoleConfig(cli="kimi", model="kimi-code/k3", effort="max")
    profiles_module.save_global([profile])

    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            # Pick the profile in the inline selector.
            app.screen.query_one("#tr-profile", Select).value = "calidad"
            await pilot.pause()

            # The implementer row got the profile values.
            form = app.screen.query_one(RolesForm)
            cli, model, effort = form.role_values("implementer")
            assert cli == "kimi"
            assert model == "kimi-code/k3"
            assert effort == "max"

            app.screen.query_one("#tr-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.profile == "calidad"
    assert reloaded.implementer.cli == "kimi"
    assert reloaded.implementer.model == "kimi-code/k3"
    assert reloaded.implementer.effort == "max"


def test_task_roles_screen_manual_change_clears_profile(monkeypatch):
    """Editing a role after picking a profile clears ``task.profile``."""
    from grafeno import profiles as profiles_module
    from grafeno.config import RoleConfig
    from grafeno.profiles import Profile

    profile = Profile(name="calidad")
    profile.implementer = RoleConfig(cli="kimi", model="kimi-code/k3", effort="max")
    profiles_module.save_global([profile])

    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            # Pick the profile, then change the implementer CLI by hand.
            app.screen.query_one("#tr-profile", Select).value = "calidad"
            await pilot.pause()
            app.screen.query_one("#implementer-cli", Select).value = "claude"
            await pilot.pause()

            app.screen.query_one("#tr-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.profile == ""  # the form no longer matches the profile
    assert reloaded.implementer.cli == "claude"


def test_task_roles_screen_profile_selector_hidden_without_profiles(monkeypatch):
    """Without global profiles, the profile selector is not shown."""
    from grafeno import profiles as profiles_module

    assert profiles_module.load_global() == []  # conftest isolates GRAFENO_HOME
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 55)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)
            assert app.screen.query_one("#tr-profile", Select).display is False

    asyncio.run(scenario())
