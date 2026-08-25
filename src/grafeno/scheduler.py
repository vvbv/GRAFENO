"""Time-scheduling, chaining and repetition logic for tasks.

Pure logic (no TUI, no asyncio) that decides which tasks are pending startup
and how to order them in a tree. Used by the App tick and by the task list.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Task, TaskState

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"  # format accepted in the form


def parse_schedule(text: str) -> str:
    """Validate ``"YYYY-MM-DD HH:MM"`` and return local ISO ``"YYYY-MM-DDTHH:MM"``.

    An empty string returns ``""`` (no schedule). Also accepts the ``"T"``
    separator. Raises ``ValueError`` if the format is invalid.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""
    candidate = cleaned.replace("T", " ")
    try:
        dt = datetime.strptime(candidate, SCHEDULE_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"Fecha/hora no válida ({text!r}); usa el formato YYYY-MM-DD HH:MM"
        ) from exc
    return dt.isoformat(timespec="minutes")


def _parse_completed_at(value: str) -> datetime | None:
    """Parse ``last_completed_at``; on failure, return ``None``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_due(task: Task, now: datetime) -> bool:
    """True if the task should start unattended RIGHT NOW.

    Conditions (all):
    - state DRAFT (never PAUSED: a pause is a user decision);
    - if it has a parent, the parent must be DONE (resolved elsewhere: see
      ``parent_done``);
    - if it has ``scheduled_at``, it must be past or present;
    - if it is interval-repetitive and has already completed once,
      ``repeat_interval_minutes`` must have passed since ``last_completed_at``.
    """
    if task.state is not TaskState.DRAFT:
        return False

    # Interval repetition: the reference is last_completed_at + interval.
    if task.repeat_mode == "interval":
        last = _parse_completed_at(task.last_completed_at)
        if last is None:
            # Never completed: falls back to scheduled_at. If absent, not due
            # (avoids looping right after creation).
            if not task.scheduled_at:
                return False
            try:
                target = datetime.fromisoformat(task.scheduled_at)
            except ValueError:
                return False
            return target <= now
        target = last + timedelta(minutes=task.repeat_interval_minutes)
        return target <= now

    # Infinite mode or no repetition: the scheduled time rules.
    if not task.scheduled_at:
        return False
    try:
        target = datetime.fromisoformat(task.scheduled_at)
    except ValueError:
        return False
    return target <= now


def parent_done(task: Task, by_id: dict[str, Task]) -> bool:
    """True if it has no parent, or its parent exists and is DONE."""
    if not task.parent_id:
        return True
    parent = by_id.get(task.parent_id)
    return parent is not None and parent.state is TaskState.DONE


def chain_completed(task: Task, by_id: dict[str, Task]) -> bool:
    """True if the task is DONE and ALL its descendants are DONE.

    Used in ``"infinite"`` mode: the repetition starts when the last task in
    the chain finishes. Returns ``False`` if any descendant is FAILED or
    DISCARDED (a broken chain does not restart on its own).
    """
    if task.state is not TaskState.DONE:
        return False
    visited: set[str] = set()

    def visit(node: Task) -> bool:
        if node.id in visited:
            return True
        visited.add(node.id)
        for candidate in by_id.values():
            if candidate.parent_id == node.id:
                if candidate.state is not TaskState.DONE:
                    return False
                if not visit(candidate):
                    return False
        return True

    return visit(task)


def children(tasks: list[Task], task_id: str) -> list[Task]:
    """Direct children of a task, preserving the order of the input list."""
    return [task for task in tasks if task.parent_id == task_id]


def tree_order(tasks: list[Task]) -> list[tuple[Task, int]]:
    """``(task, depth)`` with each child right after its parent.

    The input list is already sorted (``list_all``: most recent first).
    Tasks whose parent is not in the list (filtered or from another project)
    are shown as roots with depth 0. It is immune to ``parent_id`` cycles.
    """
    by_parent: dict[str, list[Task]] = {}
    for task in tasks:
        by_parent.setdefault(task.parent_id, []).append(task)
    ids = {task.id for task in tasks}

    roots = [task for task in tasks if task.parent_id not in ids]
    result: list[tuple[Task, int]] = []
    visited: set[str] = set()

    def visit(task: Task, depth: int) -> None:
        if task.id in visited:
            return
        visited.add(task.id)
        result.append((task, depth))
        for child in by_parent.get(task.id, []):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    return result


def prepare_next_iteration(task: Task) -> None:
    """Reset the state machine for the next repetition.

    Leaves ``state=DRAFT``, ``iteration=0``, ``cycle=1``, ``sessions={}``.
    Does NOT touch plan files: the caller decides based on ``plan_reuse``.
    The ``repeat_count`` field is incremented by the caller (not this
    function).
    """
    task.state = TaskState.DRAFT
    task.iteration = 0
    task.cycle = 1
    task.sessions = {}


# States that seal a chain position: nothing can be chained after them.
TERMINAL_STATES = (TaskState.DONE, TaskState.DISCARDED)


def rechain_error(task: Task, parent_id: str, by_id: dict[str, Task]) -> str:
    """Validate chaining ``task`` after ``parent_id``.

    Returns an i18n error key (``"et.error.*"``) when the position is
    invalid, or ``""`` when the rechain is allowed. ``parent_id == ""``
    (unchain, become a root) is always valid. The task's own state is never
    part of the validation: rechaining must not affect it.

    Rules:
    - the parent must exist and cannot be the task itself;
    - the parent cannot be a descendant of the task (cycle);
    - the parent cannot be completed (DONE/DISCARDED): you can only chain
      right after a task that has not completed yet;
    - no existing child of the parent can be completed: you cannot chain
      into a position where completed tasks already sit, only make room
      between tasks not yet processed.
    """
    if not parent_id:
        return ""
    parent = by_id.get(parent_id)
    if parent is None:
        return "et.error.parent_missing"
    if parent_id == task.id:
        return "et.error.parent_self"
    # Cycle check: walk up the ancestor chain of the candidate parent.
    seen: set[str] = set()
    cursor = parent
    while cursor.parent_id:
        if cursor.parent_id == task.id:
            return "et.error.parent_cycle"
        if cursor.parent_id in seen:
            break  # pre-existing cycle elsewhere: not ours to fix
        seen.add(cursor.parent_id)
        cursor = by_id.get(cursor.parent_id)
        if cursor is None:
            break  # orphan chain: no cycle with task
    if parent.state in TERMINAL_STATES:
        return "et.error.parent_completed"
    for candidate in by_id.values():
        if candidate.parent_id == parent_id and candidate.state in TERMINAL_STATES:
            return "et.error.position_completed"
    return ""


def rechain_candidates(task: Task, tasks: list[Task]) -> list[Task]:
    """Tasks that can be the new parent of ``task``, preserving input order."""
    by_id = {item.id: item for item in tasks}
    return [
        item
        for item in tasks
        if item.id != task.id and not rechain_error(task, item.id, by_id)
    ]
