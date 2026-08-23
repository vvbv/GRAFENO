"""Smoke tests de la TUI (Textual headless)."""

from __future__ import annotations

import asyncio

from grafeno.app import GrafenoApp
from grafeno.tui.screens.tasks import NewTaskScreen, TaskListScreen
from textual.widgets import DataTable, Input


def test_app_boots_into_task_list():
    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert app.screen.query_one(DataTable).row_count == 0

    asyncio.run(scenario())


def test_create_task_via_modal():
    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)

            app.screen.query_one("#nt-name", Input).value = "Tarea de prueba"
            await pilot.click("#nt-create")
            await pilot.pause()

            # Tras crear, se abre el detalle de la tarea.
            assert isinstance(app.screen, TaskDetailScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert app.screen.query_one(DataTable).row_count == 1

    asyncio.run(scenario())


def test_open_task_detail():
    async def scenario():
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#nt-name", Input).value = "Detalle"
            await pilot.click("#nt-create")
            await pilot.pause()
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            # Volvemos a la lista.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_detail_screen_with_dotted_filenames():
    """Regresión: nombres como `01-modulo-cache.md` no deben romper el detalle."""
    async def scenario():
        from grafeno import models, paths
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import TaskDetailScreen

        task = Task.create("Demo puntos", "desc", "/tmp", Config())
        models.save(task)
        (paths.plan_dir(task.id) / "01-modulo-cache.md").write_text("# Plan\n", encoding="utf-8")
        (paths.review_dir(task.id) / "01-review.md").write_text("# Review\n", encoding="utf-8")

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            from textual.widgets import ListView

            plan_list = app.screen.query_one("#plan-files", ListView)
            review_list = app.screen.query_one("#review-files", ListView)
            assert len(plan_list) == 1
            assert len(review_list) == 1

    asyncio.run(scenario())


def test_phase_actions_require_confirmation():
    """Las teclas de fase abren un modal de confirmación; nada arranca directo."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import PhaseConfirmScreen, TaskDetailScreen

        task = Task.create("Demo confirm", "desc", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            screen = TaskDetailScreen(models.load(task.id))
            started: list[str] = []
            screen._start = lambda runner, label, plan_then_ask=False: started.append(label)
            app.push_screen(screen)
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, PhaseConfirmScreen)
            assert started == []

            # Cancelar no ejecuta nada.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)
            assert started == []

            # Aceptar sí ejecuta.
            await pilot.press("p")
            await pilot.pause()
            await pilot.click("#pc-accept")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)
            assert started == ["Plan"]

    asyncio.run(scenario())


def test_ask_more_starts_new_cycle():
    """'Pedir más' registra la ampliación y arranca el ciclo con la misma lógica."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import RequestMoreScreen, TaskDetailScreen
        from textual.widgets import TextArea

        task = Task.create("Demo más", "desc", "/tmp", Config())
        task.state = TaskState.DONE
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            screen = TaskDetailScreen(models.load(task.id))
            started: list[str] = []
            screen._start = lambda runner, label, plan_then_ask=False: started.append(label)
            app.push_screen(screen)
            await pilot.pause()

            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, RequestMoreScreen)

            app.screen.query_one("#rm-text", TextArea).text = "Añade también exportación a CSV"
            await pilot.click("#rm-accept")
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.cycle == 2
            assert app.screen.current_task.state is TaskState.DRAFT
            assert started and "Ciclo 2" in started[0]

            reloaded = models.load(task.id)
            assert reloaded.current_extension == "Añade también exportación a CSV"

    asyncio.run(scenario())


def test_activity_bar_renders_phase_and_time():
    """La barra de actividad muestra fase en curso, eventos y tiempos."""
    async def scenario():
        import time

        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import TaskDetailScreen
        from textual.widgets import Static

        task = Task.create("Demo actividad", "desc", "/tmp", Config())
        task.durations["plan"] = 65
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            screen = app.screen
            runtime = screen.runtime

            # En espera: muestra el total acumulado.
            content = screen.query_one("#activity-bar", Static).render()
            assert "1m 05s" in (content.plain if hasattr(content, "plain") else str(content))

            # Fase en curso: muestra etiqueta, contador y totales.
            runtime.running = True
            runtime.phase_label = "Implementación"
            runtime.phase_started_at = time.monotonic()
            runtime.last_activity = time.monotonic()
            runtime.event_count = 7
            screen._render_activity()
            content = screen.query_one("#activity-bar", Static).render()
            text = content.plain if hasattr(content, "plain") else str(content)
            assert "Implementación" in text
            assert "7 eventos" in text
            assert "total" in text

            runtime.running = False
            runtime.phase_started_at = None
            screen._render_activity()
            content = screen.query_one("#activity-bar", Static).render()
            text = content.plain if hasattr(content, "plain") else str(content)
            assert "En espera" in text

    asyncio.run(scenario())


def test_navigation_does_not_interrupt_pipeline():
    """Volver al listado no interrumpe: el pipeline sigue en la App y la
    lista muestra el indicador ▶; al reabrir se reconecta."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import TaskDetailScreen
        from grafeno.tui.screens.tasks import TaskListScreen
        from textual.widgets import DataTable

        task = Task.create("Demo paralela", "desc", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            screen = app.screen

            gate = asyncio.Event()

            async def fake_runner(orch):
                orch._set_state(TaskState.IMPLEMENTING)
                await gate.wait()

            screen._start(fake_runner, "Implementación")
            await pilot.pause()
            runtime = app.runtimes[task.id]
            assert runtime.running

            # Volver a la lista NO interrumpe.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert runtime.running

            # La lista marca la tarea en ejecución con ▶.
            app.screen._reload()
            table = app.screen.query_one(DataTable)
            assert "▶" in str(table.get_row_at(0)[0])

            # Reabrir: se reconecta al mismo runtime con el log acumulado.
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            assert app.screen.runtime is runtime
            assert runtime.running

            gate.set()
            for _ in range(50):
                await pilot.pause(0.1)
                if not runtime.running:
                    break
            assert not runtime.running

    asyncio.run(scenario())
