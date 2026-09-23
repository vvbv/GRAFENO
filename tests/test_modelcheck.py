"""Tests of the configured-model availability check."""

from __future__ import annotations

import asyncio

from grafeno import modelcheck, models
from grafeno.config import Config
from grafeno.modelcheck import ModelIssue
from grafeno.profiles import Profile


def test_role_items_covers_the_five_roles():
    items = modelcheck.role_items(Config())
    assert [name for name, _ in items] == list(modelcheck.ROLE_NAMES)
    assert all(hasattr(role, "cli") for _, role in items)


def test_collect_config_roles_includes_telegram_parser_only_when_explicit():
    cfg = Config()
    pairs = modelcheck.collect_config_roles(cfg)
    assert [pair.role for pair in pairs] == list(modelcheck.ROLE_NAMES)

    cfg.telegram.parser_model = "acme/parser-1"
    pairs = modelcheck.collect_config_roles(cfg)
    parser = pairs[-1]
    assert parser.role == "telegram_parser"
    assert parser.cli == cfg.planner.cli  # falls back to the planner CLI
    assert parser.model == "acme/parser-1"

    cfg.telegram.parser_cli = "kimi"
    parser = modelcheck.collect_config_roles(cfg)[-1]
    assert parser.cli == "kimi"


def test_collect_profile_roles_carries_the_profile_name_as_context():
    profile = Profile(name="fast")
    profile.planner.model = "acme/x"
    pairs = modelcheck.collect_profile_roles([profile])
    assert len(pairs) == 5
    planner = next(pair for pair in pairs if pair.role == "planner")
    assert planner.context == "fast"
    assert planner.model == "acme/x"


def test_collect_task_roles_uses_the_task_snapshot():
    cfg = Config()
    cfg.implementer.model = "acme/impl"
    task = models.Task.create("demo", "desc", ".", cfg)
    pairs = modelcheck.collect_task_roles(task)
    impl = next(pair for pair in pairs if pair.role == "implementer")
    assert impl.model == "acme/impl"
    assert impl.context == ""


def test_used_clis_only_pairs_with_explicit_model_and_cli():
    pairs = [
        ModelIssue("planner", "opencode", "acme/x"),
        ModelIssue("reviewer", "opencode", ""),     # CLI default: not checkable
        ModelIssue("implementer", "", "acme/y"),    # blank CLI: not checkable
    ]
    assert modelcheck.used_clis(pairs) == {"opencode"}


# ---------------------------------------------------------------------- #
# find_missing
# ---------------------------------------------------------------------- #
def test_find_missing_flags_removed_models():
    pairs = [
        ModelIssue("planner", "opencode", "acme/gone"),
        ModelIssue("reviewer", "opencode", "acme/alive"),
    ]
    available = {"opencode": ["acme/alive", "acme/other"]}
    issues = modelcheck.find_missing(pairs, available)
    assert [issue.model for issue in issues] == ["acme/gone"]


def test_find_missing_skips_default_models_and_unknown_clis():
    pairs = [
        ModelIssue("planner", "opencode", ""),          # CLI default
        ModelIssue("reviewer", "kimi", "acme/x"),       # CLI list not fetched
        ModelIssue("implementer", "codex", "acme/y"),   # empty list = failed fetch
    ]
    available = {"codex": []}
    assert modelcheck.find_missing(pairs, available) == []


def test_find_missing_preserves_the_profile_context():
    pairs = [ModelIssue("planner", "opencode", "acme/gone", context="fast")]
    issues = modelcheck.find_missing(pairs, {"opencode": ["acme/other"]})
    assert issues[0].context == "fast"


# ---------------------------------------------------------------------- #
# format_issues
# ---------------------------------------------------------------------- #
def test_format_issues_groups_roles_sharing_a_pair():
    issues = [
        ModelIssue("planner", "opencode", "acme/gone"),
        ModelIssue("reviewer", "opencode", "acme/gone"),
        ModelIssue("implementer", "opencode", "acme/gone"),  # duplicate role too
        ModelIssue("implementer", "kimi", "acme/other"),
    ]
    lines = modelcheck.format_issues(issues)
    assert len(lines) == 2
    assert "Plan, Review, Implementation" in lines[0]
    assert "opencode/acme/gone" in lines[0]
    assert "kimi/acme/other" in lines[1]


def test_format_issues_mentions_the_profile():
    issues = [ModelIssue("planner", "opencode", "acme/gone", context="fast")]
    (line,) = modelcheck.format_issues(issues)
    assert "fast" in line
    assert "opencode/acme/gone" in line


def test_role_label_translates_roles_and_parser():
    assert ModelIssue("planner", "c", "m").role_label == "Plan"
    assert ModelIssue("telegram_parser", "c", "m").role_label == "Telegram parser"


# ---------------------------------------------------------------------- #
# App startup worker
# ---------------------------------------------------------------------- #
def test_app_models_check_notifies_and_caches(monkeypatch, tmp_path):
    """On boot the app fetches CLI models and warns about removed ones."""
    from grafeno import config as config_module
    from grafeno.app import GrafenoApp

    cfg = config_module.load()
    cfg.planner.model = "removed/model-x"
    cfg.reviewer.model = "removed/model-x"
    config_module.save(cfg)

    async def scenario():
        from grafeno import drivers

        monkeypatch.setattr(drivers, "available_clis", lambda: ["opencode"])

        async def fake_fetch(clis):
            return {"opencode": ["other/model"]}

        monkeypatch.setattr(drivers, "fetch_all_models", fake_fetch)
        messages: list[str] = []
        monkeypatch.setattr(
            GrafenoApp, "notify",
            lambda self, message, **kwargs: messages.append(message),
        )
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            for _ in range(100):
                await pilot.pause(0.05)
                if messages:
                    break
            assert app.available_models == {"opencode": ["other/model"]}
            assert len(messages) == 1  # planner + reviewer grouped in one line
            assert "removed/model-x" in messages[0]
            assert "Plan" in messages[0] and "Review" in messages[0]

    asyncio.run(scenario())


def test_app_models_check_quiet_when_everything_available(monkeypatch):
    from grafeno import config as config_module
    from grafeno.app import GrafenoApp

    cfg = config_module.load()
    cfg.planner.model = "acme/alive"
    config_module.save(cfg)

    async def scenario():
        from grafeno import drivers

        monkeypatch.setattr(drivers, "available_clis", lambda: ["opencode"])

        async def fake_fetch(clis):
            return {"opencode": ["acme/alive"]}

        monkeypatch.setattr(drivers, "fetch_all_models", fake_fetch)
        messages: list[str] = []
        monkeypatch.setattr(
            GrafenoApp, "notify",
            lambda self, message, **kwargs: messages.append(message),
        )
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            for _ in range(60):
                await pilot.pause(0.05)
                if app.available_models:
                    break
            await pilot.pause(0.2)
            assert app.available_models == {"opencode": ["acme/alive"]}
            assert messages == []

    asyncio.run(scenario())


# ---------------------------------------------------------------------- #
# Task detail banner
# ---------------------------------------------------------------------- #
def test_detail_banner_flags_task_with_removed_model(monkeypatch):
    async def scenario():
        from textual.widgets import Static

        from grafeno.app import GrafenoApp
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import TaskDetailScreen

        # The banner test drives ``available_models`` by hand: neutralize
        # the startup worker so it cannot overwrite it with a real fetch.
        monkeypatch.setattr(
            GrafenoApp, "_models_check", lambda self, cfg: asyncio.sleep(0)
        )
        cfg = Config()
        cfg.planner.model = "gone/model"
        task = Task.create("Modelo retirado", "desc", "/tmp", cfg)
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            app.available_models = {"opencode": ["other/model"]}
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            screen = app.screen
            screen._render_models_warning()
            widget = screen.query_one("#models-warning", Static)
            assert widget.styles.display == "block"
            rendered = str(widget.render())
            assert "gone/model" in rendered
            assert "Plan" in rendered

            # The banner hides when the model becomes available again.
            app.available_models = {"opencode": ["gone/model"]}
            screen._render_models_warning()
            assert widget.styles.display == "none"

    asyncio.run(scenario())


def test_detail_banner_hidden_without_check_data(monkeypatch):
    """No startup check data (or a failed fetch) shows no banner."""
    async def scenario():
        from textual.widgets import Static

        from grafeno.app import GrafenoApp
        from grafeno.config import Config
        from grafeno.models import Task
        from grafeno.tui.screens.detail import TaskDetailScreen

        monkeypatch.setattr(
            GrafenoApp, "_models_check", lambda self, cfg: asyncio.sleep(0)
        )
        cfg = Config()
        cfg.planner.model = "gone/model"
        task = Task.create("Sin datos", "desc", "/tmp", cfg)
        models.save(task)

        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            app.push_screen(TaskDetailScreen(models.load(task.id)))
            await pilot.pause()
            widget = app.screen.query_one("#models-warning", Static)
            assert widget.styles.display == "none"

    asyncio.run(scenario())
