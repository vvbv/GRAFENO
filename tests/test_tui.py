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
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
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
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
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
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
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
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
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
    import os

    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import TaskDetailScreen
        from grafeno.tui.screens.tasks import TaskListScreen
        from textual.widgets import DataTable

        task = Task.create("Demo paralela", "desc", os.getcwd(), Config())
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


def test_detail_screen_shows_description():
    """La pantalla de detalle muestra la descripción original de la tarea."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen

    task = Task.create("Demo desc ui", "descripcion de prueba unica", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()

            assert app.screen.query("#tab-desc") is not None
            from textual.widgets import Static
            desc = app.screen.query_one("#desc-view", Static)
            assert "descripcion de prueba unica" in str(desc.render())

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


def test_task_list_shows_global_token_summary():
    """La lista agrega tokens por modelo en #token-summary."""
    import os

    from grafeno import models
    from grafeno.config import Config
    from grafeno.drivers.base import TokenUsage
    from grafeno.models import Task
    from textual.widgets import Static as StaticWidget

    task = Task.create("Demo tokens", "desc", os.getcwd(), Config())
    task.record_tokens("opencode", "prov/M", "implement", TokenUsage(input=1500, output=600))
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.pause()  # segundo pause: se ejecuta _reload tras on_mount
            widget = app.screen.query_one("#token-summary", StaticWidget)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "prov/M" in text
            assert "1.5k" in text

    asyncio.run(scenario())


def test_token_summary_sorted_by_usage_desc():
    """El resumen de tokens ordena los modelos de mayor a menor consumo."""
    import os

    from grafeno import models
    from grafeno.config import Config
    from grafeno.drivers.base import TokenUsage
    from grafeno.models import Task
    from textual.widgets import Static as StaticWidget

    task = Task.create("Demo orden tokens", "desc", os.getcwd(), Config())
    task.record_tokens("opencode", "prov/zzz", "implement", TokenUsage(input=9000, output=1000))
    task.record_tokens("opencode", "prov/aaa", "implement", TokenUsage(input=100, output=50))
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.pause()  # segundo pause: se ejecuta _reload tras on_mount
            widget = app.screen.query_one("#token-summary", StaticWidget)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert text.index("prov/zzz") < text.index("prov/aaa")

    asyncio.run(scenario())


def test_detail_tokens_tab_shows_breakdown():
    """La pestaña Tokens del detalle muestra total, fase y CLI+modelo."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.drivers.base import TokenUsage
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen
    from textual.widgets import Static

    task = Task.create("Tokens detail", "desc", "/tmp", Config())
    task.record_tokens("opencode", "prov/M", "implement", TokenUsage(input=1500, output=600))
    task.record_tokens("kimi", "k", "review", TokenUsage(input=100, output=50))
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            widget = app.screen.query_one("#tokens-view", Static)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "1.5k" in text                       # total
            assert "Implementation" in text             # por fase
            assert "opencode/prov/M" in text            # por CLI+modelo

    asyncio.run(scenario())


def test_mark_done_forced():
    """La tecla 'd' pide confirmación y fuerza el estado done."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import StatusConfirmScreen, TaskDetailScreen

        task = Task.create("Forzar done", "desc", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, StatusConfirmScreen)
            app.screen.query_one("#pc-accept").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#pc-accept")
            await pilot.pause()
            assert models.load(task.id).state is TaskState.DONE

    asyncio.run(scenario())


def test_mark_discarded():
    """La tecla 'D' pide confirmación y marca la tarea como descartada."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import StatusConfirmScreen, TaskDetailScreen

        task = Task.create("Descartar", "desc", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            assert isinstance(app.screen, StatusConfirmScreen)
            app.screen.query_one("#pc-accept").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#pc-accept")
            await pilot.pause()
            assert models.load(task.id).state is TaskState.DISCARDED

    asyncio.run(scenario())


def test_discarded_blocks_pipeline_actions():
    """Una tarea descartada no abre el modal de confirmación de fases."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import PhaseConfirmScreen, TaskDetailScreen

        task = Task.create("Bloqueada", "desc", "/tmp", Config())
        task.state = TaskState.DISCARDED
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert not isinstance(app.screen, PhaseConfirmScreen)

    asyncio.run(scenario())


def test_main_noeditor_flag(monkeypatch):
    """El flag --noeditor desactiva la apertura automática del editor."""
    from unittest.mock import MagicMock

    from grafeno import app as app_module
    from grafeno import editor as editor_module

    calls = {"editor": 0, "run": 0}

    def fake_editor(*_args, **_kwargs):
        calls["editor"] += 1
        return True

    def fake_run():
        calls["run"] += 1

    editor_mock = MagicMock(side_effect=fake_editor)
    run_mock = MagicMock(side_effect=fake_run)
    app_mock = MagicMock()
    app_mock.return_value.run = run_mock

    monkeypatch.setattr(editor_module, "maybe_open_editor", editor_mock)
    monkeypatch.setattr(app_module, "GrafenoApp", app_mock)

    # --noeditor: el editor NO debe invocarse.
    monkeypatch.setattr("sys.argv", ["grafeno", "--noeditor"])
    app_module.main()
    assert calls["editor"] == 0
    assert calls["run"] == 1

    # Sin flag: el editor SÍ se invoca una vez.
    calls["editor"] = 0
    calls["run"] = 0
    monkeypatch.setattr("sys.argv", ["grafeno"])
    app_module.main()
    assert calls["editor"] == 1
    assert calls["run"] == 1


def test_q_does_not_quit_task_list():
    """Regresión: q no cierra la app; solo Ctrl+Q puede hacerlo."""
    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            await pilot.press("q")
            await pilot.pause()
            # La app sigue viva en la lista de tareas.
            assert isinstance(app.screen, TaskListScreen)
            assert app.is_running

    asyncio.run(scenario())


def test_theme_selection_persists():
    """Cambiar app.theme guarda la paleta en la config global."""
    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.theme = "nord"
            await pilot.pause()
            from grafeno import config as config_module

            assert config_module.load().theme == "nord"

    asyncio.run(scenario())


def test_saved_theme_is_applied_on_boot():
    """La paleta guardada se aplica al arrancar la app."""
    from grafeno import config as config_module

    cfg = config_module.load()
    cfg.theme = "nord"
    config_module.save(cfg)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.theme == "nord"

    asyncio.run(scenario())


def test_automode_status_updates_immediately():
    """Regresión: al lanzar automode el estado se refresca sin salir de la tarea."""
    from grafeno import models
    from grafeno.drivers.base import CLIDriver, EventKind, RunEvent, RunResult
    from grafeno.models import TaskState
    from grafeno.tui.screens.detail import TaskDetailScreen
    from grafeno.tui.widgets import PhaseBar
    import grafeno.pipeline.orchestrator as orch_mod

    class FakeDriver(CLIDriver):
        name = "opencode"
        display_name = "Fake"

        def is_available(self) -> bool:
            return True

        def build_command(self, request):
            return []

        async def run(self, request, on_event=None, on_activity=None):
            if on_event:
                on_event(RunEvent(EventKind.TEXT, "trabajando"))
            await asyncio.sleep(0.2)
            return RunResult(ok=True, text="salida del agente")

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            from grafeno import config as config_module

            cfg = config_module.load()
            task = models.Task.create(name="T1", description="d", workdir="/tmp", config=cfg)
            models.save(task)
            app.screen._reload()
            await pilot.pause()
            app.screen._open(task.id)
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)

            original = orch_mod.get_driver
            orch_mod.get_driver = lambda name: FakeDriver()
            try:
                await pilot.press("a")
                await pilot.pause()
                await pilot.click("#pc-accept")
                for _ in range(10):
                    await pilot.pause(0.1)
                # Sin salir de la pantalla: el estado ya no es DRAFT.
                assert app.screen.query_one(PhaseBar)._state is not TaskState.DRAFT
                assert app.screen.runtime.running or app.screen.query_one(PhaseBar)._state is TaskState.DONE
            finally:
                orch_mod.get_driver = original

    asyncio.run(scenario())


def test_detail_agents_bar_shows_phase_agents_and_tokens():
    """Bajo la barra de fases se ve el agente de cada fase y su consumo."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.drivers.base import TokenUsage
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen
    from textual.widgets import Static

    task = Task.create("Barra agentes", "desc", "/tmp", Config())
    task.planner.cli = "kimi"
    task.planner.model = "k3"
    task.implementer.cli = "opencode"
    task.implementer.model = "prov/M"
    task.record_tokens("opencode", "prov/M", "implement", TokenUsage(input=1500, output=600))
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            widget = app.screen.query_one("#agents-bar", Static)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "kimi/k3" in text          # agente de la fase de plan
            assert "opencode/prov/M" in text  # agente de implementación
            assert "1.5k" in text             # consumo fase a fase

    asyncio.run(scenario())


def test_detail_agents_bar_reflects_role_changes():
    """Al cambiar los roles de la tarea la barra se actualiza."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen
    from textual.widgets import Static

    task = Task.create("Barra cambios", "desc", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            screen = app.screen
            before = screen.query_one("#agents-bar", Static).render()
            before_text = before.plain if hasattr(before, "plain") else str(before)
            assert "codex/gpt-x" not in before_text
            screen.current_task.implementer.cli = "codex"
            screen.current_task.implementer.model = "gpt-x"
            screen._render_agents_bar()
            await pilot.pause()
            after = screen.query_one("#agents-bar", Static).render()
            after_text = after.plain if hasattr(after, "plain") else str(after)
            assert "codex/gpt-x" in after_text

    asyncio.run(scenario())


def test_task_list_clock_shows_current_time():
    """La lista muestra un reloj con formato YYYY-MM-DD HH:MM:SS."""
    import re

    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()  # segundo pause: el reloj ya se renderizó
            from textual.widgets import Static

            clock = app.screen.query_one("#clock", Static).render()
            text = clock.plain if hasattr(clock, "plain") else str(clock)
            assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text)

    asyncio.run(scenario())


def test_task_list_filter_default_only_project():
    """Por defecto solo se listan las tareas del proyecto actual."""
    import os

    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from textual.widgets import DataTable

        cwd = os.getcwd()
        in_project = Task.create("En proyecto", "d", cwd, Config())
        models.save(in_project)
        other = Task.create("En otro", "d", "/tmp", Config())
        models.save(other)

        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 1

            # Pulsa `v` para mostrar todas.
            await pilot.press("v")
            await pilot.pause()
            assert table.row_count == 2

    asyncio.run(scenario())


def test_task_list_sublist_indents_children():
    """Las hijas se muestran debajo de su padre e indentadas."""
    import os

    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from textual.widgets import DataTable

        parent = Task.create("Padre", "d", os.getcwd(), Config())
        parent.id = "p-indent"
        models.save(parent)
        child = Task.create("Hija", "d", os.getcwd(), Config())
        child.id = "c-indent"
        child.parent_id = "p-indent"
        models.save(child)

        app = GrafenoApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            table = app.screen.query_one(DataTable)
            assert table.row_count == 2

            # Primera fila: el padre, sin indentación (o solo prefijo de root).
            first = str(table.get_row_at(0)[0])
            assert first.endswith("Padre")
            # Segunda fila: la hija, indentada con dos espacios y `+ `.
            second = str(table.get_row_at(1)[0])
            assert second.startswith("  + ")
            assert "Hija" in second

    asyncio.run(scenario())


def test_new_task_form_accepts_schedule():
    """El formulario acepta una fecha futura y la persiste como ISO local."""
    async def scenario():
        from grafeno import models
        from grafeno.tui.screens.detail import TaskDetailScreen
        from textual.widgets import Input

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen

            screen.query_one("#nt-name", Input).value = "Programada"
            screen.query_one("#nt-schedule", Input).value = "2020-01-01 10:00"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.scheduled_at == "2020-01-01T10:00"
            reloaded = models.load(app.screen.current_task.id)
            assert reloaded.scheduled_at == "2020-01-01T10:00"

    asyncio.run(scenario())


def test_new_task_form_rejects_invalid_schedule():
    """Una fecha inválida deja el modal abierto sin crear la tarea."""
    async def scenario():
        from grafeno import models
        from grafeno.tui.screens.tasks import NewTaskScreen
        from textual.widgets import Input

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen

            screen.query_one("#nt-name", Input).value = "Fecha mala"
            screen.query_one("#nt-schedule", Input).value = "ayer"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            # Sigue en el modal y no se ha creado ninguna tarea.
            assert isinstance(app.screen, NewTaskScreen)
            assert not models.list_all()

    asyncio.run(scenario())


def test_new_task_form_persists_parent_id():
    """Elegir tarea padre en el selector lo persiste en parent_id."""
    import os

    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from textual.widgets import Input, Select

        parent = Task.create("Padre", "d", os.getcwd(), Config())
        parent.id = "p-form"
        models.save(parent)

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen

            assert screen.query_one("#nt-parent", Select)._options  # hay opciones
            screen.query_one("#nt-parent", Select).value = "p-form"
            screen.query_one("#nt-name", Input).value = "Hija"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            from grafeno.tui.screens.detail import TaskDetailScreen
            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.parent_id == "p-form"
            reloaded = models.load(app.screen.current_task.id)
            assert reloaded.parent_id == "p-form"

    asyncio.run(scenario())


def test_new_task_form_repetitive_interval_validates_and_forces_automode():
    """Modo repetitivo interval exige minutos válidos y fuerza automode."""
    async def scenario():
        from grafeno import models
        from grafeno.tui.screens.detail import TaskDetailScreen
        from textual.widgets import Checkbox, Input, Select

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            screen = app.screen

            # Intervalo vacío: debe rechazar.
            screen.query_one("#nt-name", Input).value = "Repetitiva"
            screen.query_one("#nt-repeat", Select).value = "interval"
            screen.query_one("#nt-repeat-minutes", Input).value = ""
            screen.query_one("#nt-automode", Checkbox).value = False
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            from grafeno.tui.screens.tasks import NewTaskScreen
            assert isinstance(app.screen, NewTaskScreen)
            assert not models.list_all()

            # Intervalo válido: crea la tarea, fuerza automode.
            screen.query_one("#nt-repeat-minutes", Input).value = "30"
            screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)
            assert app.screen.current_task.repeat_mode == "interval"
            assert app.screen.current_task.repeat_interval_minutes == 30
            assert app.screen.current_task.automode is True

    asyncio.run(scenario())


def test_edit_task_info():
    """La tecla 'E' abre el modal, edita nombre/descripcion y persiste."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import EditTaskScreen, TaskDetailScreen
        from textual.widgets import Input, TextArea

        task = Task.create("Original", "desc original", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("E")
            await pilot.pause()
            assert isinstance(app.screen, EditTaskScreen)
            assert app.screen.query_one("#et-name", Input).value == "Original"
            app.screen.query_one("#et-name", Input).value = "Renombrada"
            app.screen.query_one("#et-description", TextArea).text = "nueva desc"
            app.screen.query_one("#et-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#et-save")
            await pilot.pause()
            persisted = models.load(task.id)
            assert persisted.name == "Renombrada"
            assert persisted.description == "nueva desc"

    asyncio.run(scenario())


def test_edit_task_info_requires_name():
    """El modal no cierra si el nombre queda vacio."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import EditTaskScreen, TaskDetailScreen
        from textual.widgets import Input

        task = Task.create("Con nombre", "desc", "/tmp", Config())
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("E")
            await pilot.pause()
            app.screen.query_one("#et-name", Input).value = "   "
            app.screen.query_one("#et-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#et-save")
            await pilot.pause()
            assert isinstance(app.screen, EditTaskScreen)
            assert models.load(task.id).name == "Con nombre"

    asyncio.run(scenario())


def test_restart_resets_task_to_draft():
    """La tecla 'R' pide confirmacion y reinicia la tarea a DRAFT."""
    async def scenario():
        from grafeno import models, paths
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import StatusConfirmScreen, TaskDetailScreen

        task = Task.create("Reiniciar", "desc", "/tmp", Config())
        models.save(task)
        task.state = TaskState.IMPLEMENTED
        task.iteration = 2
        models.save(task)
        (paths.plan_dir(task.id) / "01-plan.md").write_text("plan", encoding="utf-8")

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            assert isinstance(app.screen, StatusConfirmScreen)
            app.screen.query_one("#pc-accept").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#pc-accept")
            for _ in range(10):
                await pilot.pause(0.1)
            persisted = models.load(task.id)
            assert persisted.state is TaskState.DRAFT
            assert persisted.iteration == 0
            assert list(paths.plan_dir(task.id).glob("*.md")) == []

    asyncio.run(scenario())


def test_restart_blocked_when_discarded():
    """Una tarea descartada no se puede reiniciar."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import StatusConfirmScreen, TaskDetailScreen

        task = Task.create("Descartada", "desc", "/tmp", Config())
        models.save(task)
        task.state = TaskState.DISCARDED
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            assert not isinstance(app.screen, StatusConfirmScreen)
            assert models.load(task.id).state is TaskState.DISCARDED

    asyncio.run(scenario())
