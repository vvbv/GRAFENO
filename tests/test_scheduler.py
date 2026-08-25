"""Tests of the scheduler module: time scheduling, chaining and repetition."""

from __future__ import annotations

import tomllib
from datetime import datetime, timedelta

import pytest

from grafeno import _toml, models, scheduler
from grafeno.config import Config
from grafeno.models import Task, TaskState


def _task(tmp_path, **overrides) -> Task:
    return Task.create("Demo", "desc", str(tmp_path), Config(), **overrides)


def test_parse_schedule_accepts_space_and_t_separator():
    assert scheduler.parse_schedule("2026-08-23 18:30") == "2026-08-23T18:30"
    assert scheduler.parse_schedule("2026-08-23T18:30") == "2026-08-23T18:30"
    assert scheduler.parse_schedule("  2026-08-23 18:30  ") == "2026-08-23T18:30"


def test_parse_schedule_empty_returns_empty():
    assert scheduler.parse_schedule("") == ""
    assert scheduler.parse_schedule("   ") == ""


def test_parse_schedule_invalid_raises():
    with pytest.raises(ValueError):
        scheduler.parse_schedule("ayer")
    with pytest.raises(ValueError):
        scheduler.parse_schedule("2026/08/23 18:30")


def test_new_fields_roundtrip(tmp_path):
    task = _task(
        tmp_path,
        scheduled_at="2026-08-23T18:30",
        parent_id="padre-1",
        repeat_mode="interval",
        repeat_interval_minutes=45,
        plan_reuse="replan",
    )
    task.repeat_count = 3
    task.last_completed_at = "2026-08-22T10:15:00"
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.scheduled_at == "2026-08-23T18:30"
    assert loaded.parent_id == "padre-1"
    assert loaded.repeat_mode == "interval"
    assert loaded.repeat_interval_minutes == 45
    assert loaded.plan_reuse == "replan"
    assert loaded.repeat_count == 3
    assert loaded.last_completed_at == "2026-08-22T10:15:00"


def test_legacy_task_toml_loads_with_defaults(tmp_path):
    """An old task.toml (without the new keys) loads with the defaults."""
    task = _task(tmp_path)
    models.save(task)
    meta = models.paths.task_meta_path(task.id) if hasattr(models, "paths") else None
    from grafeno import paths

    with paths.task_meta_path(task.id).open("rb") as handle:
        data = tomllib.load(handle)
    data["task"].pop("scheduled_at", None)
    data["task"].pop("parent_id", None)
    data["task"].pop("repeat_mode", None)
    data["task"].pop("repeat_interval_minutes", None)
    data["task"].pop("plan_reuse", None)
    data["task"].pop("repeat_count", None)
    data["task"].pop("last_completed_at", None)
    paths.task_meta_path(task.id).write_text(_toml.dumps(data), encoding="utf-8")

    legacy = models.load(task.id)
    assert legacy.scheduled_at == ""
    assert legacy.parent_id == ""
    assert legacy.repeat_mode == ""
    assert legacy.repeat_interval_minutes == 60
    assert legacy.plan_reuse == "reuse"
    assert legacy.repeat_count == 0
    assert legacy.last_completed_at == ""


def test_is_due_past_scheduled_at_draft_returns_true(tmp_path):
    task = _task(tmp_path, scheduled_at="2020-01-01T10:00")
    assert scheduler.is_due(task, datetime(2026, 1, 1))


def test_is_due_future_scheduled_at_returns_false(tmp_path):
    task = _task(tmp_path, scheduled_at="2999-01-01T10:00")
    assert not scheduler.is_due(task, datetime(2026, 1, 1))


def test_is_due_paused_returns_false_even_when_due(tmp_path):
    task = _task(tmp_path, scheduled_at="2020-01-01T10:00")
    task.state = TaskState.PAUSED
    assert not scheduler.is_due(task, datetime(2026, 1, 1))


def test_is_due_interval_recent_completion_not_due(tmp_path):
    task = _task(tmp_path, repeat_mode="interval", repeat_interval_minutes=60)
    task.last_completed_at = "2026-01-01T10:00:00"
    assert not scheduler.is_due(task, datetime(2026, 1, 1, 10, 30))


def test_is_due_interval_old_completion_is_due(tmp_path):
    task = _task(tmp_path, repeat_mode="interval", repeat_interval_minutes=60)
    task.last_completed_at = "2026-01-01T10:00:00"
    assert scheduler.is_due(task, datetime(2026, 1, 1, 11, 30))


def test_is_due_interval_without_completion_uses_scheduled_at(tmp_path):
    task = _task(
        tmp_path,
        repeat_mode="interval",
        repeat_interval_minutes=60,
        scheduled_at="2020-01-01T10:00",
    )
    assert scheduler.is_due(task, datetime(2026, 1, 1))


def test_is_due_interval_without_completion_or_schedule_not_due(tmp_path):
    task = _task(tmp_path, repeat_mode="interval", repeat_interval_minutes=60)
    assert not scheduler.is_due(task, datetime(2026, 1, 1))


def test_parent_done_no_parent_returns_true(tmp_path):
    task = _task(tmp_path)
    assert scheduler.parent_done(task, {})


def test_parent_done_parent_completed(tmp_path):
    parent = _task(tmp_path)
    parent.id = "parent-1"
    parent.name = "Padre"
    parent.state = TaskState.DONE
    child = _task(tmp_path)
    child.id = "child-1"
    child.name = "Hija"
    child.parent_id = parent.id
    assert scheduler.parent_done(child, {parent.id: parent, child.id: child})


def test_parent_done_parent_implementing_returns_false(tmp_path):
    parent = _task(tmp_path)
    parent.id = "parent-2"
    parent.name = "Padre"
    parent.state = TaskState.IMPLEMENTING
    child = _task(tmp_path)
    child.id = "child-2"
    child.name = "Hija"
    child.parent_id = parent.id
    assert not scheduler.parent_done(child, {parent.id: parent, child.id: child})


def test_tree_order_basic(tmp_path):
    parent = _task(tmp_path)
    parent.id = "p1"
    parent.name = "Padre"
    child_a = _task(tmp_path)
    child_a.id = "ca"
    child_a.name = "HijaA"
    child_a.parent_id = parent.id
    grandchild = _task(tmp_path)
    grandchild.id = "gc"
    grandchild.name = "Nieta"
    grandchild.parent_id = child_a.id
    child_b = _task(tmp_path)
    child_b.id = "cb"
    child_b.name = "HijaB"
    child_b.parent_id = parent.id
    ordered = scheduler.tree_order([parent, child_a, grandchild, child_b])
    assert [(t.name, d) for t, d in ordered] == [
        ("Padre", 0),
        ("HijaA", 1),
        ("Nieta", 2),
        ("HijaB", 1),
    ]


def test_tree_order_orphan_is_root(tmp_path):
    orphan = _task(tmp_path)
    orphan.id = "orphan-1"
    orphan.name = "Huérfana"
    orphan.parent_id = "no-existe"
    ordered = scheduler.tree_order([orphan])
    assert [(t.name, d) for t, d in ordered] == [("Huérfana", 0)]


def test_tree_order_cycle_does_not_hang(tmp_path):
    a = _task(tmp_path)
    a.id = "a"
    a.name = "A"
    b = _task(tmp_path)
    b.id = "b"
    b.name = "B"
    a.parent_id = b.id
    b.parent_id = a.id
    # Must not raise RecursionError nor hang the UI.
    ordered = scheduler.tree_order([a, b])
    # Each task appears at most once.
    assert len(ordered) <= 2


def test_chain_completed_done_chain(tmp_path):
    parent = _task(tmp_path)
    parent.id = "p-chain"
    parent.name = "Padre"
    parent.state = TaskState.DONE
    child = _task(tmp_path)
    child.id = "c-chain"
    child.name = "Hija"
    child.parent_id = parent.id
    child.state = TaskState.DONE
    assert scheduler.chain_completed(parent, {parent.id: parent, child.id: child})


def test_chain_completed_implementing_returns_false(tmp_path):
    parent = _task(tmp_path)
    parent.id = "p-impl"
    parent.name = "Padre"
    parent.state = TaskState.DONE
    child = _task(tmp_path)
    child.id = "c-impl"
    child.name = "Hija"
    child.parent_id = parent.id
    child.state = TaskState.IMPLEMENTING
    assert not scheduler.chain_completed(parent, {parent.id: parent, child.id: child})


def test_chain_completed_failed_returns_false(tmp_path):
    parent = _task(tmp_path)
    parent.id = "p-fail"
    parent.name = "Padre"
    parent.state = TaskState.DONE
    child = _task(tmp_path)
    child.id = "c-fail"
    child.name = "Hija"
    child.parent_id = parent.id
    child.state = TaskState.FAILED
    assert not scheduler.chain_completed(parent, {parent.id: parent, child.id: child})


def test_chain_completed_not_done_returns_false(tmp_path):
    parent = _task(tmp_path)
    parent.state = TaskState.IMPLEMENTING
    assert not scheduler.chain_completed(parent, {parent.id: parent})


def test_children_keeps_input_order(tmp_path):
    parent = _task(tmp_path)
    parent.id = "p-children"
    parent.name = "Padre"
    a = _task(tmp_path)
    a.id = "child-a"
    a.name = "A"
    a.parent_id = parent.id
    b = _task(tmp_path)
    b.id = "child-b"
    b.name = "B"
    b.parent_id = parent.id
    found = scheduler.children([a, b, parent], parent.id)
    assert [t.name for t in found] == ["A", "B"]


def test_prepare_next_iteration_resets_machine(tmp_path):
    task = _task(tmp_path)
    task.state = TaskState.DONE
    task.iteration = 3
    task.cycle = 5
    task.sessions = {"planner": "ses-1"}
    task.repeat_count = 7
    scheduler.prepare_next_iteration(task)
    assert task.state is TaskState.DRAFT
    assert task.iteration == 0
    assert task.cycle == 1
    assert task.sessions == {}
    assert task.repeat_count == 7  # the increment is done by the caller


# ---------------------------------------------------------------------- #
# Rechain validation: changing the parent_id of an existing task.
# ---------------------------------------------------------------------- #


def _chain_tasks(tmp_path) -> dict[str, Task]:
    """Four tasks: root -> mid -> leaf, plus a free task."""
    root = _task(tmp_path)
    root.id = "root"
    root.name = "root"
    mid = _task(tmp_path, parent_id=root.id)
    mid.id = "mid"
    mid.name = "mid"
    leaf = _task(tmp_path, parent_id=mid.id)
    leaf.id = "leaf"
    leaf.name = "leaf"
    free = _task(tmp_path)
    free.id = "free"
    free.name = "free"
    return {t.id: t for t in (root, mid, leaf, free)}


def test_rechain_error_blank_parent_always_valid(tmp_path):
    by_id = _chain_tasks(tmp_path)
    free = by_id["free"]
    for state in (
        TaskState.DRAFT,
        TaskState.DONE,
        TaskState.DISCARDED,
        TaskState.PAUSED,
        TaskState.FAILED,
    ):
        free.state = state
        assert scheduler.rechain_error(free, "", by_id) == ""


def test_rechain_error_missing_parent(tmp_path):
    by_id = _chain_tasks(tmp_path)
    free = by_id["free"]
    assert scheduler.rechain_error(free, "no-existe", by_id) == "et.error.parent_missing"


def test_rechain_error_self_parent(tmp_path):
    by_id = _chain_tasks(tmp_path)
    free = by_id["free"]
    assert scheduler.rechain_error(free, free.id, by_id) == "et.error.parent_self"


def test_rechain_error_cycle(tmp_path):
    by_id = _chain_tasks(tmp_path)
    root = by_id["root"]
    leaf = by_id["leaf"]
    assert scheduler.rechain_error(root, leaf.id, by_id) == "et.error.parent_cycle"


def test_rechain_error_completed_parent(tmp_path):
    by_id = _chain_tasks(tmp_path)
    free = by_id["free"]
    mid = by_id["mid"]
    for terminal in (TaskState.DONE, TaskState.DISCARDED):
        mid.state = terminal
        assert (
            scheduler.rechain_error(free, mid.id, by_id) == "et.error.parent_completed"
        )


def test_rechain_error_completed_sibling_seals_position(tmp_path):
    by_id = _chain_tasks(tmp_path)
    root = by_id["root"]
    mid = by_id["mid"]
    free = by_id["free"]
    mid.state = TaskState.DONE
    root.state = TaskState.DRAFT
    assert (
        scheduler.rechain_error(free, root.id, by_id) == "et.error.position_completed"
    )


def test_rechain_error_valid_between_unprocessed(tmp_path):
    by_id = _chain_tasks(tmp_path)
    root = by_id["root"]
    mid = by_id["mid"]
    free = by_id["free"]
    root.state = TaskState.DRAFT
    mid.state = TaskState.DRAFT
    # mid is already a child of root, both unprocessed: making room
    # between unprocessed tasks is valid.
    assert scheduler.rechain_error(free, root.id, by_id) == ""


def test_rechain_error_valid_after_failed_parent(tmp_path):
    by_id = _chain_tasks(tmp_path)
    free = by_id["free"]
    mid = by_id["mid"]
    for state in (TaskState.FAILED, TaskState.PAUSED):
        mid.state = state
        assert scheduler.rechain_error(free, mid.id, by_id) == ""


def test_rechain_candidates_excludes_invalid(tmp_path):
    by_id = _chain_tasks(tmp_path)
    root = by_id["root"]
    mid = by_id["mid"]
    free = by_id["free"]
    leaf = by_id["leaf"]
    # Seal the position under root: mid is DONE.
    mid.state = TaskState.DONE
    root.state = TaskState.DRAFT

    candidates = scheduler.rechain_candidates(free, list(by_id.values()))
    candidate_ids = {item.id for item in candidates}
    # root's position is sealed, so it must NOT appear.
    assert root.id not in candidate_ids
    # free cannot chain after itself.
    assert free.id not in candidate_ids
    # leaf is non-terminal: it must appear.
    assert leaf.id in candidate_ids
