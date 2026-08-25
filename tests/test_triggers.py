"""Tests of the triggers module and its persistence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from grafeno import models, triggers
from grafeno.config import Config, PROJECT_CONFIG_FILE
from grafeno.triggers import ALL_PHASES, TRIGGER_STAGES, Trigger, parse_phases


def test_trigger_dict_roundtrip():
    """Trigger roundtrips through to_dict/from_dict."""
    trigger = Trigger(
        name="x", description="d", phases="plan,review", timing="before", workdir="/w"
    )
    data = trigger.to_dict()
    assert data == {
        "name": "x",
        "description": "d",
        "phases": "plan,review",
        "timing": "before",
        "workdir": "/w",
    }
    assert Trigger.from_dict(data) == trigger


def test_trigger_from_dict_defaults():
    """Missing keys default to phases='all' and timing='after'."""
    trigger = Trigger.from_dict({})
    assert trigger.name == ""
    assert trigger.description == ""
    assert trigger.phases == "all"
    assert trigger.timing == "after"
    assert trigger.workdir == ""


def test_trigger_from_dict_invalid_timing_falls_back_to_after():
    """An unknown timing falls back to 'after' (tolerant parsing)."""
    trigger = Trigger.from_dict({"timing": "bogus"})
    assert trigger.timing == "after"


def test_trigger_from_dict_empty_phases_falls_back_to_all():
    """An empty phases value falls back to 'all'."""
    trigger = Trigger.from_dict({"phases": ""})
    assert trigger.phases == "all"


def test_parse_phases_all_returns_every_stage():
    """'all' returns the full TRIGGER_STAGES list, in order."""
    assert parse_phases("all") == list(TRIGGER_STAGES)


def test_parse_phases_subset_keeps_canonical_order():
    """Subset keeps the canonical order of TRIGGER_STAGES and ignores unknowns."""
    assert parse_phases("review,plan") == ["plan", "review"]
    assert parse_phases("inventada,plan,zzz") == ["plan"]


def test_matches_stage_and_timing():
    """matches is True only when both stage and timing match."""
    trigger = Trigger(name="x", phases="plan,review", timing="after")
    assert triggers.matches(trigger, "plan", "after") is True
    assert triggers.matches(trigger, "review", "after") is True
    assert triggers.matches(trigger, "implement", "after") is False
    assert triggers.matches(trigger, "plan", "before") is False


def test_matches_all_phases_matches_any_stage():
    """phases='all' matches every TRIGGER_STAGES stage."""
    trigger = Trigger(name="x", phases=ALL_PHASES, timing="after")
    for stage in TRIGGER_STAGES:
        assert triggers.matches(trigger, stage, "after") is True


def test_save_load_global_roundtrip():
    """Saving and reloading returns the same list of triggers."""
    triggers_list = [
        Trigger(name="a", description="da", phases="all", timing="before"),
        Trigger(name="b", phases="plan,final", timing="after", workdir="/w"),
    ]
    triggers.save_global(triggers_list)
    loaded = triggers.load_global()
    assert loaded == triggers_list


def test_load_global_missing_returns_empty():
    """If the global file does not exist, returns []."""
    assert triggers.load_global() == []


def test_load_project_tolerant(tmp_path):
    """Project triggers coexist with [editor] and [[references]]; bad/missing = []."""
    from grafeno import _toml

    payload = {
        "editor": {"enabled": True, "editor": "code", "mode": "window", "side": "left"},
        "references": [{"name": "p1", "path": "/p1"}],
        "triggers": [
            {"name": "pt1", "description": "d", "phases": "plan", "timing": "before"},
            {"name": "pt2", "phases": "all", "timing": "after", "workdir": "/p2"},
        ],
    }
    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps(payload), encoding="utf-8")
    loaded = triggers.load_project(tmp_path)
    assert loaded == [
        Trigger(name="pt1", description="d", phases="plan", timing="before"),
        Trigger(name="pt2", phases="all", timing="after", workdir="/p2"),
    ]
    assert triggers.load_project(tmp_path / "missing") == []
    (tmp_path / PROJECT_CONFIG_FILE).write_text("not valid toml [[[", encoding="utf-8")
    assert triggers.load_project(tmp_path) == []


def test_resolve_global_first_then_project(tmp_path):
    """resolve concatenates global + project, global first."""
    triggers.save_global([Trigger(name="g1", phases="all", timing="after")])
    from grafeno import _toml

    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps({
        "triggers": [Trigger(name="p1", phases="plan", timing="before").to_dict()],
    }), encoding="utf-8")
    resolved = triggers.resolve(tmp_path)
    assert [t.name for t in resolved] == ["g1", "p1"]


def test_task_origin_roundtrip():
    """Task.origin survives to_dict/from_dict."""
    task = models.Task(id="t", name="n")
    assert task.origin == ""
    loaded = models.Task.from_dict(task.to_dict())
    assert loaded.origin == ""


def test_spawn_creates_scheduled_automode_task():
    """spawn creates an independent task with origin='trigger' and automode=True."""
    cfg = Config()
    cfg.automode.confirm_plan = True
    parent = models.Task.create("Parent", "d", ".", cfg)
    parent_id_before = parent.id

    spawned = triggers.spawn(Trigger(name="ping", description="d"), parent)

    assert spawned is not parent
    assert spawned.id != parent_id_before
    assert spawned.name == "ping"
    assert spawned.description == "d"
    assert spawned.origin == triggers.ORIGIN_TRIGGER
    assert spawned.automode is True
    assert spawned.confirm_plan is False
    assert spawned.scheduled_at  # non-empty
    datetime.fromisoformat(spawned.scheduled_at)  # parseable
    assert spawned.workdir == parent.workdir
    # Persisted on disk.
    reloaded = models.load(spawned.id)
    assert reloaded.origin == "trigger"
    assert reloaded.automode is True


def test_spawn_uses_trigger_workdir_when_set():
    """An explicit trigger.workdir overrides the parent task workdir."""
    parent = models.Task.create("Parent", "d", "/parent", Config())
    spawned = triggers.spawn(
        Trigger(name="x", description="", workdir="/otro"),
        parent,
    )
    assert spawned.workdir == "/otro"


def test_fire_filters_and_reports():
    """fire spawns only matching triggers and calls on_info with the localized message."""
    triggers.save_global([
        Trigger(name="match", phases="plan", timing="after"),
        Trigger(name="nope", phases="implement", timing="after"),
        Trigger(name="wrong-timing", phases="all", timing="before"),
    ])
    parent = models.Task.create("Parent", "d", ".", Config())
    models.save(parent)
    infos: list[str] = []

    spawned_count = triggers.fire(parent, "plan", "after", on_info=infos.append)

    assert spawned_count == 1
    assert len(infos) == 1
    assert 'match' in infos[0]
    assert 'Plan' in infos[0]
    spawned = models.list_all()
    trigger_tasks = [t for t in spawned if t.origin == "trigger"]
    assert len(trigger_tasks) == 1
    assert trigger_tasks[0].name == "match"


def test_fire_does_not_recurse_on_trigger_tasks():
    """A task with origin='trigger' never fires triggers itself."""
    triggers.save_global([Trigger(name="ping", phases="all", timing="after")])
    parent = models.Task.create("Parent", "d", ".", Config())
    parent.origin = "trigger"
    models.save(parent)
    before = len([t for t in models.list_all() if t.origin == "trigger"])

    spawned = triggers.fire(parent, "plan", "after", on_info=lambda m: None)

    assert spawned == 0
    after = len([t for t in models.list_all() if t.origin == "trigger"])
    assert after == before


def test_fire_best_effort_on_spawn_failure(monkeypatch):
    """If spawn raises, fire continues and reports via on_info (no exception leaks)."""
    triggers.save_global([Trigger(name="boom", phases="all", timing="after")])
    parent = models.Task.create("Parent", "d", ".", Config())
    models.save(parent)

    def _boom(trigger, task):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(triggers, "spawn", _boom)
    infos: list[str] = []

    spawned = triggers.fire(parent, "plan", "after", on_info=infos.append)

    assert spawned == 0
    assert any("boom" in m for m in infos)
    assert any("disk on fire" in m for m in infos)
