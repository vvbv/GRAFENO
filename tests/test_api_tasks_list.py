"""Tests of the task list contract: filters, ordering and pagination."""

from __future__ import annotations

import pytest

from grafeno import scheduler
from grafeno.config import Config
from grafeno.models import Task, TaskState
from grafeno.server import actions
from grafeno.server.actions import ApiError


class _Service:
    app = None


def _task(index: int, workdir: str, state: TaskState, completed: str = "") -> Task:
    task = Task.create(f"Task {index}", "d", workdir, Config())
    task.id = f"20260101-{index:06d}-task-{index}"
    task.state = state
    task.created_at = f"2026-01-01T{index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}"
    task.updated_at = task.created_at
    task.last_completed_at = completed
    return task


def _patch(monkeypatch, tasks: list[Task]) -> list[Task]:
    ordered = sorted(tasks, key=lambda task: task.id, reverse=True)  # like list_all
    monkeypatch.setattr(actions.models, "list_all", lambda: ordered)
    return ordered


@pytest.fixture
def many_tasks(monkeypatch) -> list[Task]:
    """300 tasks: 294 DONE over two projects plus a few pending ones."""
    tasks: list[Task] = []
    for index in range(294):
        workdir = "/work/GRAFENO" if index % 4 == 0 else "/work/Other"
        # Completion order deliberately differs from creation order.
        completed = f"2026-02-01T{(293 - index) // 60:02d}:{(293 - index) % 60:02d}:00"
        tasks.append(_task(index, workdir, TaskState.DONE, completed))
    for index in range(294, 300):
        tasks.append(_task(index, "/work/GRAFENO", TaskState.DRAFT))
    return _patch(monkeypatch, tasks)


def _list(**kwargs) -> dict:
    return actions.list_tasks(_Service(), **kwargs)


def test_default_list_returns_everything(many_tasks) -> None:
    data = _list()
    assert len(data["tasks"]) == 300
    assert data["total"] == 300
    assert data["limit"] is None
    assert data["has_more"] is False
    assert all("completed_at" in item and "depth" in item for item in data["tasks"])


def test_default_order_is_unchanged(many_tasks) -> None:
    expected = [task.id for task, _ in scheduler.tree_order(many_tasks)]
    assert [item["id"] for item in _list()["tasks"]] == expected


def test_filter_state_and_project(many_tasks) -> None:
    data = _list(state="done", project="grafeno")
    assert data["total"] == 74
    assert all(item["workdir"] == "/work/GRAFENO" and item["state"] == "done" for item in data["tasks"])


def test_filter_workdir_trailing_slash(many_tasks) -> None:
    assert _list(workdir="/work/GRAFENO/")["total"] == _list(project="GRAFENO")["total"] == 80


def test_project_does_not_match_prefix(many_tasks, monkeypatch) -> None:
    extra = _task(500, "/work/GRAFENO-CLOUD", TaskState.DONE, "2026-03-01T00:00:00")
    _patch(monkeypatch, [*many_tasks, extra])
    data = _list(project="GRAFENO")
    assert extra.id not in [item["id"] for item in data["tasks"]]
    assert data["total"] == 80


def test_multi_state_filter(many_tasks) -> None:
    assert _list(state="done,draft")["total"] == 300
    with pytest.raises(ApiError) as info:
        _list(state="done, bogus")
    assert info.value.status == 400


def test_last_three_completed_of_project(many_tasks) -> None:
    data = _list(state="done", project="GRAFENO", sort="completed", limit=3)
    assert len(data["tasks"]) == 3
    assert data["has_more"] is True
    stamps = [item["completed_at"] for item in data["tasks"]]
    assert stamps == sorted(stamps, reverse=True) and len(set(stamps)) == 3
    done = [t for t in many_tasks if t.state is TaskState.DONE and t.workdir == "/work/GRAFENO"]
    expected = sorted(done, key=lambda task: task.last_completed_at, reverse=True)[:3]
    assert [item["id"] for item in data["tasks"]] == [task.id for task in expected]


def test_pagination_covers_all_without_duplicates(many_tasks) -> None:
    full = [item["id"] for item in _list(sort="completed")["tasks"]]
    seen: list[str] = []
    pages = 0
    offset = 0
    while True:
        data = _list(sort="completed", limit=50, offset=offset)
        seen.extend(item["id"] for item in data["tasks"])
        pages += 1
        offset += len(data["tasks"])
        if not data["has_more"]:
            break
    assert pages == 6
    assert len(set(seen)) == len(seen)
    assert seen == full


def test_offset_past_end(many_tasks) -> None:
    data = _list(offset=1000)
    assert data["tasks"] == []
    assert data["total"] == 300
    assert data["has_more"] is False


def test_sort_order_asc_and_undated_last(many_tasks) -> None:
    asc = _list(sort="completed", order="asc")["tasks"]
    stamps = [item["completed_at"] for item in asc[:294]]
    assert stamps == sorted(stamps) and all(stamps)
    assert all(item["state"] == "draft" and item["completed_at"] == "" for item in asc[294:])
    desc = _list(sort="completed", order="desc")["tasks"]
    assert all(item["state"] == "draft" for item in desc[294:])
    assert all(item["state"] == "done" for item in desc[:294])


def test_completed_at_falls_back_to_updated_at(monkeypatch) -> None:
    done = _task(1, "/work/A", TaskState.DONE)
    draft = _task(2, "/work/A", TaskState.DRAFT)
    _patch(monkeypatch, [done, draft])
    by_id = {item["id"]: item for item in _list()["tasks"]}
    assert by_id[done.id]["completed_at"] == done.updated_at
    assert by_id[draft.id]["completed_at"] == ""


def test_flat_sort_depth_zero(monkeypatch) -> None:
    parent = _task(1, "/work/A", TaskState.DONE)
    child = _task(2, "/work/A", TaskState.DRAFT)
    child.parent_id = parent.id
    _patch(monkeypatch, [parent, child])
    assert all(item["depth"] == 0 for item in _list(sort="created")["tasks"])
    depths = {item["id"]: item["depth"] for item in _list(sort="tree")["tasks"]}
    assert depths[child.id] == 1


def test_done_hidden_ids_ignore_pagination(many_tasks) -> None:
    assert _list(limit=1)["done_hidden_ids"] == _list()["done_hidden_ids"]


@pytest.mark.parametrize(
    "kwargs",
    [{"sort": "name"}, {"order": "up"}, {"limit": 0}, {"limit": 1001}, {"offset": -1}, {"state": "nope"}],
)
def test_invalid_params(many_tasks, kwargs) -> None:
    with pytest.raises(ApiError) as info:
        _list(**kwargs)
    assert info.value.status == 400


def test_schedule_payload_future_paused_and_waiting(monkeypatch):
    future = _task(1, "/work/A", TaskState.DRAFT)
    future.scheduled_at = "2999-01-02T10:00"
    paused = _task(2, "/work/A", TaskState.PAUSED)
    paused.scheduled_at = "2999-01-02T10:00"
    parent = _task(3, "/work/A", TaskState.DRAFT)
    parent.name = "Parent"
    child = _task(4, "/work/A", TaskState.DRAFT)
    child.parent_id = parent.id
    _patch(monkeypatch, [future, paused, parent, child])
    items = {item["id"]: item for item in _list()["tasks"]}
    sched = items[future.id]["schedule"]
    assert sched["status"] == "future"
    assert sched["at"] == "2999-01-02T10:00"
    assert sched["at_iso"][-6] in "+-" and sched["at_iso"][-3] == ":"
    assert sched["utc_offset"].startswith("UTC")
    stale = items[paused.id]
    assert stale["schedule"]["status"] == ""
    assert stale["scheduled_at"] == "2999-01-02T10:00"
    waiting = items[child.id]["schedule"]
    assert waiting["status"] == "waiting_parent"
    assert waiting["parent_name"] == "Parent"


def test_task_detail_includes_schedule(monkeypatch):
    task = _task(1, "/work/A", TaskState.DRAFT)
    task.scheduled_at = "2999-01-02T10:00"
    _patch(monkeypatch, [task])
    monkeypatch.setattr(actions.models, "load", lambda task_id: task)
    detail = actions.get_task(_Service(), task.id)["task"]
    assert detail["schedule"]["status"] == "future"
