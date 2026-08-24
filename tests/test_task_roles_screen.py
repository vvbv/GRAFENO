"""Tests del modal de agentes por tarea (modelos mockeados, sin CLIs reales)."""

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
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            # Espera a que llegue el catálogo de modelos.
            for _ in range(50):
                await pilot.pause(0.1)
                if app.screen.query_one(RolesForm).models:
                    break

            # Cambia el planificador a kimi + modelo concreto.
            app.screen.query_one("#planner-cli", Select).value = "kimi"
            await pilot.pause()
            app.screen.query_one("#planner-model", Select).value = "kimi-code/k3"

            app.screen.query_one("#tr-save").scroll_visible()
            await pilot.pause()
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.planner.cli == "kimi"
    assert reloaded.planner.model == "kimi-code/k3"
    # El resto de roles no se tocan.
    assert reloaded.implementer.cli == task.implementer.cli
    assert reloaded.reviewer.model == task.reviewer.model


def test_task_roles_screen_cancel_keeps_roles(monkeypatch):
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()
    original_cli = task.planner.cli

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)

            app.screen.query_one("#tr-cancel").scroll_visible()
            await pilot.pause()
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
        # Forzamos español para que la aserción del texto sea estable.
        i18n.set_language("es")
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(task))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)
            await asyncio.wait_for(started.wait(), timeout=2)

            await pilot.press("escape")  # cancela la carga, NO cierra
            await pilot.pause()
            assert isinstance(app.screen, TaskRolesScreen)
            status = app.screen.query_one("#roles-status", Static).render()
            status_text = status.plain if hasattr(status, "plain") else str(status)
            assert "cancelada" in status_text
            release.set()

            await pilot.press("escape")  # ahora sí cierra sin guardar
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())


def test_task_roles_screen_persists_effort(monkeypatch):
    """El esfuerzo seleccionado en el modal se guarda en task.toml."""
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_models", _fake_fetch)
    monkeypatch.setattr("grafeno.tui.screens.roles.fetch_all_variants", _fake_fetch_variants)
    task = _make_task()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 45)) as pilot:
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

            form = app.screen.query_one(RolesForm)
            form.set_role("planner", "opencode", "opencode-go/kimi-k3", "low")
            await pilot.pause()

            app.screen.query_one("#tr-save").scroll_visible()
            await pilot.pause()
            await pilot.click("#tr-save")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

    asyncio.run(scenario())

    reloaded = models.load(task.id)
    assert reloaded.planner.effort == "low"
