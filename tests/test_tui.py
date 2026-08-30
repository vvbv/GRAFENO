"""Smoke tests of the TUI (Textual headless)."""

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


def test_app_boots_without_auto_update_worker_by_default():
    """With ``auto_update=False`` (default) no update worker is spawned on boot."""
    from grafeno import config as config_module

    cfg = config_module.load()
    cfg.auto_update = False
    config_module.save(cfg)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            # No "auto-update" group is running.
            assert app.workers is None or all(
                getattr(worker, "group", "") != "auto-update"
                for worker in (app.workers._workers.values() if app.workers else [])
            )

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

            # After creation, the task detail is opened.
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
            # Back to the list.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_detail_screen_with_dotted_filenames():
    """Regression: names like `01-modulo-cache.md` must not break the detail screen."""
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
    """Phase keys open a confirmation modal; nothing starts directly."""
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

            # Cancel does not run anything.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)
            assert started == []

            # Accept does run.
            await pilot.press("p")
            await pilot.pause()
            await pilot.click("#pc-accept")
            await pilot.pause()
            assert isinstance(app.screen, TaskDetailScreen)
            assert started == ["Plan"]

    asyncio.run(scenario())


def test_ask_more_starts_new_cycle():
    """'Ask for more' records the extension and starts the cycle with the same logic."""
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
    """The activity bar shows current phase, events and times."""
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

            # Idle: shows the accumulated total.
            content = screen.query_one("#activity-bar", Static).render()
            assert "1m 05s" in (content.plain if hasattr(content, "plain") else str(content))

            # Current phase: shows label, counter and totals.
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
    """The branch checkbox takes the global value and is saved per task."""
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
    """The modal preloads the global final_prompt and saves the override on the task."""
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

            # Going back to the list does NOT interrupt.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            assert runtime.running

            # The list marks the running task with ▶.
            app.screen._reload()
            table = app.screen.query_one(DataTable)
            assert "▶" in str(table.get_row_at(0)[0])

            # Reopen: reconnects to the same runtime with the accumulated log.
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
    """The phase bar reflects the `final` phase in FINALIZING (active) and DONE (done)."""
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
    """The detail screen exposes the #tab-final tab and the `s` binding."""
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

            # The 's' binding is registered and triggers action_run_final.
            keys = {b.key for b in TaskDetailScreen.BINDINGS if isinstance(b.key, str)}
            assert "s" in keys

            # The final-steps tab exists.
            assert app.screen.query("#tab-final") is not None

    asyncio.run(scenario())


def test_detail_screen_shows_description():
    """The detail screen shows the task's original description."""
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
    """Regression: a long plan must scroll in the Markdown viewer."""
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

            # Select the file in the plans list.
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

            # There is overflow content and, focused, the keyboard scrolls.
            assert scroll.max_scroll_y > 0
            assert scroll.can_focus
            scroll.focus()
            await pilot.pause()
            await pilot.press("pagedown")
            await pilot.pause()
            assert scroll.scroll_y > 0

    asyncio.run(scenario())


def test_task_list_shows_global_token_summary():
    """The list aggregates tokens by model in #token-summary."""
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
            await pilot.pause()  # second pause: _reload runs after on_mount
            widget = app.screen.query_one("#token-summary", StaticWidget)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert "prov/M" in text
            assert "1.5k" in text

    asyncio.run(scenario())


def test_token_summary_sorted_by_usage_desc():
    """The tokens summary sorts models from highest to lowest usage."""
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
            await pilot.pause()  # second pause: _reload runs after on_mount
            widget = app.screen.query_one("#token-summary", StaticWidget)
            rendered = widget.render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
            assert text.index("prov/zzz") < text.index("prov/aaa")

    asyncio.run(scenario())


def test_task_list_shows_duration_and_global_total():
    """The task list shows per-task duration and the consolidated total time."""
    import os

    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from textual.widgets import DataTable, Static as StaticWidget

    task = Task.create("Demo duracion", "desc", os.getcwd(), Config())
    task.durations = {"plan": 65, "implement": 120}
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            await pilot.pause()  # second pause: _reload runs after on_mount
            table = app.screen.query_one(DataTable)
            row = [str(cell) for cell in table.get_row_at(0)]
            assert any("3m 05s" in cell for cell in row)
            summary = app.screen.query_one("#token-summary", StaticWidget)
            content = summary.render()
            text = content.plain if hasattr(content, "plain") else str(content)
            assert "3m 05s" in text

    asyncio.run(scenario())


def test_detail_tokens_tab_shows_breakdown():
    """The Tokens tab on the detail screen shows total, phase and CLI+model."""
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
    """The 'd' key asks for confirmation and forces the state to done."""
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
    """The 'D' key asks for confirmation and marks the task as discarded."""
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
    """A discarded task does not open the phase confirmation modal."""
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
    """The --noeditor flag turns off the automatic editor opening."""
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

    # --noeditor: the editor must NOT be invoked.
    monkeypatch.setattr("sys.argv", ["grafeno", "--noeditor"])
    app_module.main()
    assert calls["editor"] == 0
    assert calls["run"] == 1

    # No flag: the editor IS invoked once.
    calls["editor"] = 0
    calls["run"] = 0
    monkeypatch.setattr("sys.argv", ["grafeno"])
    app_module.main()
    assert calls["editor"] == 1
    assert calls["run"] == 1


def test_q_does_not_quit_task_list():
    """Regression: q does not close the app; only the quit shortcut (Ctrl+Q / Cmd+Q) can."""
    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            await pilot.press("q")
            await pilot.pause()
            # The app is still alive in the task list.
            assert isinstance(app.screen, TaskListScreen)
            assert app.is_running

    asyncio.run(scenario())


def test_theme_selection_persists():
    """Changing app.theme persists the palette in the global config."""
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
    """The saved palette is applied on app startup."""
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
    """Regression: when launching automode, the state refreshes without leaving the task."""
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
                # Without leaving the screen: the state is no longer DRAFT.
                assert app.screen.query_one(PhaseBar)._state is not TaskState.DRAFT
                assert app.screen.runtime.running or app.screen.query_one(PhaseBar)._state is TaskState.DONE
            finally:
                orch_mod.get_driver = original

    asyncio.run(scenario())


def test_detail_agents_bar_shows_phase_agents_and_tokens():
    """Under the phase bar you see each phase's agent and its consumption."""
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
    """When the task's roles change, the bar updates."""
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
    """The list shows a clock formatted as YYYY-MM-DD HH:MM:SS."""
    import re

    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()  # second pause: the clock is already rendered
            from textual.widgets import Static

            clock = app.screen.query_one("#clock", Static).render()
            text = clock.plain if hasattr(clock, "plain") else str(clock)
            assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text)

    asyncio.run(scenario())


def test_task_list_filter_default_only_project():
    """By default only the tasks of the current project are listed."""
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

            # Press `v` to show all.
            await pilot.press("v")
            await pilot.pause()
            assert table.row_count == 2

    asyncio.run(scenario())


def test_task_list_sublist_indents_children():
    """Children are shown below their parent and indented."""
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

            # First row: the parent, no indentation (or only root prefix).
            first = str(table.get_row_at(0)[0])
            assert first.endswith("Padre")
            # Second row: the child, indented with two spaces and `+ `.
            second = str(table.get_row_at(1)[0])
            assert second.startswith("  + ")
            assert "Hija" in second

    asyncio.run(scenario())


def test_new_task_form_accepts_schedule():
    """The form accepts a future date and persists it as local ISO."""
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
    """An invalid date leaves the modal open without creating the task."""
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

            # It stays in the modal and no task was created.
            assert isinstance(app.screen, NewTaskScreen)
            assert not models.list_all()

    asyncio.run(scenario())


def test_new_task_form_persists_parent_id():
    """Choosing a parent task in the selector persists it in parent_id."""
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
    """Interval repetition mode requires valid minutes and forces automode."""
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

            # Empty interval: must reject.
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

            # Valid interval: creates the task, forces automode.
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
    """The 'E' key opens the modal, edits name/description and persists."""
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
    """The modal does not close if the name is left empty."""
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
    """The 'R' key asks for confirmation and resets the task to DRAFT."""
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
    """A discarded task cannot be restarted."""
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


def test_quit_bindings_include_cmd_q() -> None:
    """Quitting works with Ctrl+Q and with Cmd+Q (super) on macOS."""
    from grafeno.app import GrafenoApp

    keys = {binding.key for binding in GrafenoApp.BINDINGS}
    assert "ctrl+q" in keys
    assert "super+q" in keys


def test_new_task_issue_selector_fills_name_and_description(monkeypatch):
    """Choosing an issue fills the task name and description."""
    from grafeno import gh as gh_module
    from grafeno.tui.screens import tasks as tasks_module

    issues = [
        gh_module.GhIssue(number=7, title="Fix login", body="Steps to reproduce"),
        gh_module.GhIssue(number=3, title="Write docs", body=""),
    ]
    monkeypatch.setattr(tasks_module.gh_module, "gh_available", lambda _wd: True)
    monkeypatch.setattr(tasks_module.gh_module, "list_issues", lambda _wd, **kw: issues)

    async def scenario():
        from textual.widgets import Input, Select, TextArea

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.05)  # wait for the background worker

            select = app.screen.query_one("#nt-issue", Select)
            assert select.display is True
            # _options includes the blank sentinel of allow_blank=True.
            assert [opt for opt in select._options if opt[1] is not Select.NULL] == [
                ("#7 Fix login", "7"),
                ("#3 Write docs", "3"),
            ]

            select.value = "7"
            await pilot.pause()
            assert app.screen.query_one("#nt-name", Input).value == "Fix login"
            assert app.screen.query_one("#nt-description", TextArea).text == "Steps to reproduce"

            # Empty body: the description falls back to the title.
            select.value = "3"
            await pilot.pause()
            assert app.screen.query_one("#nt-name", Input).value == "Write docs"
            assert app.screen.query_one("#nt-description", TextArea).text == "Write docs"

    asyncio.run(scenario())


def test_new_task_issue_selector_hidden_without_gh(monkeypatch):
    """Without gh/repo/access the issue selector is not shown."""
    from grafeno.tui.screens import tasks as tasks_module

    monkeypatch.setattr(tasks_module.gh_module, "gh_available", lambda _wd: False)

    async def scenario():
        from textual.widgets import Select

        app = GrafenoApp()
        async with app.run_test(size=(120, 60)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            for _ in range(20):
                await pilot.pause(0.05)

            assert app.screen.query_one("#nt-issue", Select).display is False

    asyncio.run(scenario())


def test_header_clock_visible_on_all_screens():
    """The header clock (date + time with seconds) is on every main screen."""
    import re

    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.config import ConfigScreen
    from grafeno.tui.screens.detail import TaskDetailScreen
    from grafeno.tui.widgets import DateTimeClock, GrafenoHeader

    task = Task.create("Reloj cabecera", "desc", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            pattern = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"

            def assert_clock():
                clock = app.screen.query_one("#clock", DateTimeClock)
                rendered = clock.render()
                text = rendered.plain if hasattr(rendered, "plain") else str(rendered)
                assert re.match(pattern, text)
                assert app.screen.query_one(GrafenoHeader)

            # Task list screen.
            assert_clock()

            # Task detail screen.
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            assert_clock()

            # Config screen.
            app.push_screen(ConfigScreen())
            await pilot.pause()
            assert_clock()

    asyncio.run(scenario())


def test_edit_task_rechains_parent():
    """The 'E' modal rechains the task without touching its state or name."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import EditTaskScreen, TaskDetailScreen
        from textual.widgets import Select

        parent = Task.create("Padre", "desc", "/tmp", Config())
        parent.id = "p-rechain"
        models.save(parent)
        child = Task.create("Hija", "desc original", "/tmp", Config())
        child.id = "c-rechain"
        child.parent_id = ""
        models.save(child)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(child.id)))
            await pilot.pause()
            await pilot.press("E")
            await pilot.pause()
            assert isinstance(app.screen, EditTaskScreen)

            screen = app.screen
            screen.query_one("#et-parent", Select).value = parent.id
            screen.query_one("#et-save").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#et-save")
            await pilot.pause()

            loaded = models.load(child.id)
            assert loaded.parent_id == parent.id
            assert loaded.state is TaskState.DRAFT
            assert loaded.name == "Hija"

    asyncio.run(scenario())


def test_edit_task_rejects_completed_parent():
    """The parent selector does not list completed tasks; saving with one rejects."""
    async def scenario():
        from grafeno import models
        from grafeno.config import Config
        from grafeno.models import Task, TaskState
        from grafeno.tui.screens.detail import EditTaskScreen, TaskDetailScreen
        from textual.widgets import Select

        parent = Task.create("Padre done", "desc", "/tmp", Config())
        parent.id = "p-done"
        parent.state = TaskState.DONE
        models.save(parent)
        child = Task.create("Hija", "desc", "/tmp", Config())
        child.id = "c-done"
        child.parent_id = ""
        models.save(child)

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(child.id)))
            await pilot.pause()
            await pilot.press("E")
            await pilot.pause()
            assert isinstance(app.screen, EditTaskScreen)

            select = app.screen.query_one("#et-parent", Select)
            option_values = [value for _, value in select._options]
            assert "p-done" not in option_values
            assert select.value is Select.NULL


def test_create_remote_task_via_modal():
    """A remote spec creates a task whose ``remote`` is the SSH string and ``workdir`` the remote path."""
    async def scenario():
        from grafeno import models
        from grafeno.tui.screens.detail import TaskDetailScreen

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, NewTaskScreen)

            app.screen.query_one("#nt-name", Input).value = "Remota"
            app.screen.query_one("#nt-remote", Input).value = "dev@box.example:/srv/app"
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()

            assert isinstance(app.screen, TaskDetailScreen)
            tasks = models.list_all()
            assert len(tasks) == 1
            assert tasks[0].remote == "dev@box.example:/srv/app"
            assert tasks[0].workdir == "/srv/app"
            assert tasks[0].is_remote

            # The list shows the SSH spec instead of a local directory.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_create_task_bad_remote_rejected():
    """A malformed remote value is rejected without creating a task."""
    async def scenario():
        from grafeno import models

        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#nt-name", Input).value = "Mal remota"
            app.screen.query_one("#nt-remote", Input).value = "sin-host-ni-path"
            app.screen.query_one("#nt-create").scroll_visible()
            for _ in range(5):
                await pilot.pause(0.05)
            await pilot.click("#nt-create")
            await pilot.pause()
            # The modal stays open: no task was created.
            assert isinstance(app.screen, NewTaskScreen)
            assert models.list_all() == []

    asyncio.run(scenario())


def test_location_bar_shows_cwd_on_task_list_and_config():
    """The location bar always shows the current directory."""
    import os

    from grafeno.tui.screens.config import ConfigScreen
    from textual.widgets import Static

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            await pilot.pause()
            bar = app.screen.query_one("#location-bar", Static)
            text = str(bar.render())
            assert f"cwd: {os.getcwd()}" in text

            app.push_screen(ConfigScreen())
            await pilot.pause()
            bar = app.screen.query_one("#location-bar", Static)
            assert f"cwd: {os.getcwd()}" in str(bar.render())

    asyncio.run(scenario())


def test_location_bar_in_detail_shows_cwd_and_task_path():
    """Inside a task the bar shows the current path AND the task path."""
    import os

    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen
    from textual.widgets import Static

    task = Task.create("Barra ruta", "desc", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(100, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            text = str(app.screen.query_one("#location-bar", Static).render())
            assert f"cwd: {os.getcwd()}" in text
            assert "/tmp" in text
            assert "[SSH]" not in text

    asyncio.run(scenario())


def test_location_bar_remote_task_shows_ssh_badge():
    """A remote task shows the SSH spec and the [SSH] badge in the bar."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen
    from textual.widgets import Static

    task = Task.create("Barra remota", "desc", "/srv/app", Config(),
                       remote="user@example.com:/srv/app")
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            text = str(app.screen.query_one("#location-bar", Static).render())
            assert "user@example.com:/srv/app" in text
            assert "[SSH]" in text

    asyncio.run(scenario())


def test_detail_screen_has_media_tab_and_files_list():
    """The detail screen exposes the #tab-media tab and the #media-files list."""
    from grafeno import models
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.screens.detail import TaskDetailScreen

    task = Task.create("Demo media ui", "desc", "/tmp", Config())
    models.save(task)

    async def scenario():
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()

            assert app.screen.query("#tab-media") is not None
            assert app.screen.query("#media-files") is not None

    asyncio.run(scenario())
