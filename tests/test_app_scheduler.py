"""Tests del scheduler de la App: tick, task_finished y reinicio de repeticiones."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from grafeno import models, paths, scheduler
from grafeno.app import GrafenoApp
from grafeno.config import Config
from grafeno.models import Task, TaskState

# Estado compartido entre invocaciones del orquestador (cada _wrap crea una
# nueva instancia, pero podemos contar cuántas veces se ha llamado).
_REEVALUATE_CALL_COUNT = 0
_REEVALUATE_GATE: asyncio.Event | None = None


class _FakeOrchestrator:
    """Orquestador de prueba: marca la tarea como DONE y notifica el callback."""

    def __init__(self, task, **callbacks):
        self.task = task
        self._on_state = callbacks.get("on_state", lambda task: None)

    async def run_automode(self):
        self.task.state = TaskState.DONE
        models.save(self.task)
        self._on_state(self.task)


class _FakeOrchestratorReevaluate:
    """Orquestador que ejecuta run_reevaluate_plan + run_automode_continue.

    La primera invocación completa marca la tarea como DONE. Las siguientes
    esperan en el gate global ``_REEVALUATE_GATE`` para no entrar en bucle
    infinito en los tests.
    """

    def __init__(self, task, **callbacks):
        self.task = task
        self._on_state = callbacks.get("on_state", lambda task: None)

    async def run_reevaluate_plan(self):
        pass

    async def run_automode_continue(self):
        global _REEVALUATE_CALL_COUNT
        _REEVALUATE_CALL_COUNT += 1
        if _REEVALUATE_CALL_COUNT == 1:
            self.task.state = TaskState.DONE
            models.save(self.task)
            self._on_state(self.task)
        elif _REEVALUATE_GATE is not None:
            await _REEVALUATE_GATE.wait()


def _make_task(tmp_path, **overrides) -> Task:
    return Task.create("Demo", "desc", str(tmp_path), Config(), **overrides)


def test_task_finished_chains_draft_child():
    async def scenario():
        from grafeno.tui.runtime import TaskRuntime

        parent = _make_task(None)
        parent.id = "parent-1"
        parent.state = TaskState.DONE
        models.save(parent)

        child = _make_task(None)
        child.id = "child-1"
        child.parent_id = parent.id
        models.save(child)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            parent_rt = TaskRuntime(parent, orchestrator_factory=_FakeOrchestrator)
            child_rt = TaskRuntime(child, orchestrator_factory=_FakeOrchestrator)
            app.runtimes[parent.id] = parent_rt
            app.runtimes[child.id] = child_rt

            app.task_finished(parent)
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.05)
                if not child_rt.running:
                    break
            assert not child_rt.running
            assert models.load(child.id).state is TaskState.DONE

        app.runtimes.clear()

    asyncio.run(scenario())


def test_task_finished_skips_child_with_future_schedule():
    async def scenario():
        from grafeno.tui.runtime import TaskRuntime

        parent = _make_task(None)
        parent.id = "parent-2"
        parent.state = TaskState.DONE
        models.save(parent)

        future = (datetime.now() + timedelta(days=1)).isoformat(timespec="minutes")
        child = _make_task(None, parent_id=parent.id, scheduled_at=future)
        child.id = "child-2"
        models.save(child)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            child_rt = TaskRuntime(child, orchestrator_factory=_FakeOrchestrator)
            app.runtimes[child.id] = child_rt

            app.task_finished(parent)
            await pilot.pause()

            assert not child_rt.running

        app.runtimes.clear()

    asyncio.run(scenario())


def test_scheduler_tick_starts_due_task():
    async def scenario():
        from grafeno.tui.runtime import TaskRuntime

        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="minutes")
        task = _make_task(None, scheduled_at=past)
        task.id = "due-1"
        models.save(task)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rt = TaskRuntime(task, orchestrator_factory=_FakeOrchestrator)
            app.runtimes[task.id] = rt

            app._scheduler_tick()
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.05)
                if not rt.running:
                    break
            assert models.load(task.id).state is TaskState.DONE

        app.runtimes.clear()

    asyncio.run(scenario())


def test_scheduler_tick_skips_paused_task():
    async def scenario():
        from grafeno.tui.runtime import TaskRuntime

        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="minutes")
        task = _make_task(None, scheduled_at=past)
        task.id = "paused-1"
        task.state = TaskState.PAUSED
        models.save(task)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rt = TaskRuntime(task, orchestrator_factory=_FakeOrchestrator)
            app.runtimes[task.id] = rt

            app._scheduler_tick()
            await pilot.pause()
            assert not rt.running

        app.runtimes.clear()

    asyncio.run(scenario())


def test_infinite_repetition_restarts_draft():
    """Modo infinite: al completarse la cadena, la tarea vuelve a DRAFT y se relanza."""
    from grafeno.tui.runtime import TaskRuntime

    async def scenario():
        global _REEVALUATE_CALL_COUNT, _REEVALUATE_GATE
        _REEVALUATE_CALL_COUNT = 0
        _REEVALUATE_GATE = asyncio.Event()

        parent = _make_task(None)
        parent.id = "inf-parent"
        parent.state = TaskState.DONE
        parent.repeat_mode = "infinite"
        parent.repeat_count = 0
        models.save(parent)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rt = TaskRuntime(parent, orchestrator_factory=_FakeOrchestratorReevaluate)
            app.runtimes[parent.id] = rt

            app.task_finished(parent)
            # Espera a que la repetición se haya reiniciado al menos una vez.
            for _ in range(40):
                await pilot.pause(0.05)
                reloaded = models.load(parent.id)
                if reloaded.state is TaskState.DRAFT and reloaded.repeat_count >= 1:
                    break
            reloaded = models.load(parent.id)
            assert reloaded.state is TaskState.DRAFT
            assert reloaded.repeat_count >= 1
            # Cierra el gate para detener el bucle de iteraciones.
            _REEVALUATE_GATE.set()
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.05)
                if not rt.running:
                    break

        app.runtimes.clear()
        _REEVALUATE_GATE = None

    asyncio.run(scenario())


def test_replan_removes_existing_plan_files():
    """Al reiniciar con plan_reuse=replan, los archivos .md del plan se borran."""
    from grafeno.tui.runtime import TaskRuntime

    async def scenario():
        task = _make_task(None, plan_reuse="replan", repeat_mode="infinite")
        task.id = "replan-1"
        plan_file = paths.plan_dir(task.id, 1) / "01-previo.md"
        plan_file.write_text("# Plan previo\n", encoding="utf-8")
        assert plan_file.exists()

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rt = TaskRuntime(task, orchestrator_factory=_FakeOrchestrator)
            app.runtimes[task.id] = rt

            app._restart_repetition(task)
            assert not plan_file.exists()

        app.runtimes.clear()

    asyncio.run(scenario())


def test_reevaluate_repetition_uses_reevaluate_runner():
    """En modo reevaluate, task_finished arranca la repetición con reevaluate_plan_prompt."""
    from grafeno.tui.runtime import TaskRuntime

    async def scenario():
        global _REEVALUATE_CALL_COUNT, _REEVALUATE_GATE
        _REEVALUATE_CALL_COUNT = 0
        _REEVALUATE_GATE = asyncio.Event()

        task = _make_task(None, plan_reuse="reevaluate", repeat_mode="infinite")
        task.id = "re-1"
        task.state = TaskState.DONE
        models.save(task)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            rt = TaskRuntime(task, orchestrator_factory=_FakeOrchestratorReevaluate)
            app.runtimes[task.id] = rt

            app.task_finished(task)
            # Espera al primer ciclo del runner (run_reevaluate_plan + continue).
            for _ in range(40):
                await pilot.pause(0.05)
                reloaded = models.load(task.id)
                if reloaded.state is TaskState.DRAFT and reloaded.repeat_count >= 1:
                    break
            reloaded = models.load(task.id)
            assert reloaded.state is TaskState.DRAFT
            assert reloaded.repeat_count >= 1
            _REEVALUATE_GATE.set()
            await pilot.pause()

        app.runtimes.clear()
        _REEVALUATE_GATE = None

    asyncio.run(scenario())

