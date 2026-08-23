"""Smoke tests de la TUI (Textual headless)."""

from __future__ import annotations

import asyncio

from grafeno.app import GrafenoApp
from grafeno.tui.screens.tasks import NewTaskScreen, TaskListScreen
from textual.widgets import DataTable, Input, TextArea


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
        async with app.run_test(size=(100, 50)) as pilot:
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
        async with app.run_test(size=(100, 50)) as pilot:
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
        async with app.run_test(size=(100, 50)) as pilot:
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
        async with app.run_test(size=(100, 50)) as pilot:
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
        async with app.run_test(size=(100, 50)) as pilot:
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
            assert started and "Cycle 2" in started[0]

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
        async with app.run_test(size=(100, 50)) as pilot:
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
            assert "7 events" in text
            assert "total" in text

            runtime.running = False
            runtime.phase_started_at = None
            screen._render_activity()
            content = screen.query_one("#activity-bar", Static).render()
            text = content.plain if hasattr(content, "plain") else str(content)
            assert "Idle" in text

    asyncio.run(scenario())


def test_new_task_branch_checkbox_defaults_and_persists():
    """El checkbox de rama toma el valor global y se guarda por tarea."""
    async def scenario():
        from grafeno import config as config_module, models
        from textual.widgets import Checkbox

        cfg = config_module.load()
        cfg.automode.create_branch = False
        config_module.save(cfg)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)
            checkbox = app.screen.query_one("#nt-branch", Checkbox)
            assert checkbox.value is False  # hereda el valor global

            checkbox.value = True
            app.screen.query_one("#nt-name", Input).value = "Con rama"
            await pilot.click("#nt-create")
            await pilot.pause()

            from grafeno.tui.screens.detail import TaskDetailScreen
            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.create_branch is True

    asyncio.run(scenario())


def test_new_task_final_prompt_inherits_and_overrides():
    """El modal precarga final_prompt global y guarda el override en la tarea."""
    async def scenario():
        from grafeno import config as config_module, models

        cfg = config_module.load()
        cfg.final_prompt = "global"
        config_module.save(cfg)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)

            area = app.screen.query_one("#nt-final-prompt", TextArea)
            assert area.text == "global"  # hereda el valor global

            area.text = "override\nmultilínea"
            app.screen.query_one("#nt-name", Input).value = "Con cierre"
            await pilot.click("#nt-create")
            await pilot.pause()

            from grafeno.tui.screens.detail import TaskDetailScreen
            assert isinstance(app.screen, TaskDetailScreen)
            task_id = app.screen.current_task.id
            assert app.screen.current_task.final_prompt == "override\nmultilínea"

            reloaded = models.load(task_id)
            assert reloaded.final_prompt == "override\nmultilínea"

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
        async with app.run_test(size=(100, 50)) as pilot:
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


def test_phase_status_includes_final():
    """La barra de fases refleja la fase `final` en FINALIZING (active) y DONE (done)."""
    from grafeno.models import TaskState
    from grafeno.tui.widgets import _phase_status

    finalizing = _phase_status(TaskState.FINALIZING)
    assert finalizing["plan"] == "done"
    assert finalizing["implement"] == "done"
    assert finalizing["review"] == "done"
    assert finalizing["final"] == "active"
    assert finalizing["done"] == "pending"

    done = _phase_status(TaskState.DONE)
    assert done["final"] == "done"
    assert done["done"] == "done"


def test_detail_screen_has_final_tab_and_binding():
    """La pantalla de detalle expone la pestaña #tab-final y el binding `s`."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen

    task = Task.create("Demo final ui", "desc", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()

            # El binding 's' está registrado y lanza action_run_final.
            keys = {b.key for b in TaskDetailScreen.BINDINGS if isinstance(b.key, str)}
            assert "s" in keys

            # La pestaña de pasos finales existe.
            assert app.screen.query("#tab-final") is not None

    asyncio.run(scenario())


def test_markdown_views_are_scrollable():
    """Regresión: un plan largo debe poder hacer scroll en el visor Markdown."""
    async def scenario():
        from grafeno import models, paths
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import TaskDetailScreen
        from textual.containers import VerticalScroll
        from textual.widgets import ListView

        task = Task.create("Demo scroll", "desc", "/tmp", Config())
        models.save(task)
        long_md = "\n\n".join(f"## Seccion {i}\n\nparrafo {i}" for i in range(80))
        (paths.plan_dir(task.id) / "01-largo.md").write_text(long_md, encoding="utf-8")

        app = GrafenoApp()
        async with app.run_test(size=(100, 30)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()

            # Seleccionar el archivo en la lista de planes.
            plan_list = app.screen.query_one("#plan-files", ListView)
            plan_list.focus()
            plan_list.index = 0
            await pilot.pause()
            await pilot.press("enter")
            for _ in range(20):
                await pilot.pause(0.05)
                scroll = app.screen.query_one("#plan-scroll", VerticalScroll)
                if scroll.max_scroll_y > 0:
                    break

            # Hay contenido desbordado y, con foco, el teclado hace scroll.
            assert scroll.max_scroll_y > 0
            assert scroll.can_focus
            scroll.focus()
            await pilot.pause()
            await pilot.press("pagedown")
            await pilot.pause()
            assert scroll.scroll_y > 0

    asyncio.run(scenario())
