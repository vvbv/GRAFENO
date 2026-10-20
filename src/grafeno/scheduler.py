"""Time-scheduling, chaining and repetition logic for tasks.

Pure logic (no TUI, no asyncio) that decides which tasks are pending startup
and how to order them in a tree. Used by the App tick and by the task list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .i18n import t
from .models import Task, TaskState
from .timefmt import format_countdown

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


def next_start(task: Task) -> datetime | None:
    """Effective next unattended start time (local, naive), or ``None``.

    Interval repetitions that already completed once restart at
    ``last_completed_at + repeat_interval_minutes``; otherwise the
    ``scheduled_at`` field rules. Ignores the task state.
    """
    if task.repeat_mode == "interval":
        last = _parse_completed_at(task.last_completed_at)
        if last is not None:
            return last + timedelta(minutes=task.repeat_interval_minutes)
    scheduled = _parse_completed_at(task.scheduled_at)
    if scheduled is not None and scheduled.tzinfo is not None:
        # Hand-edited task.toml with an offset: convert to naive local time.
        scheduled = scheduled.astimezone().replace(tzinfo=None)
    return scheduled


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
    target = next_start(task)
    return target is not None and target <= now


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


# States the "hide completed" toggle may hide: terminal, with no pending work.
_HIDEABLE_STATES = (TaskState.DONE, TaskState.DISCARDED)


def done_hidden_ids(tasks: list[Task]) -> set[str]:
    """Ids of the finished tasks that may be hidden without breaking a chain.

    Finished means DONE or DISCARDED. A chain is the connected component of
    the ``parent_id`` forest that contains the task (ancestors, descendants
    and kin linked through a common ancestor). A finished task is hideable
    only when EVERY task of its component is finished; if any member still
    has pending work (e.g. DRAFT, FAILED, PAUSED), the whole component stays
    visible. Immune to ``parent_id`` cycles.
    """
    by_id = {task.id: task for task in tasks}
    neighbors: dict[str, set[str]] = {task.id: set() for task in tasks}
    for task in tasks:
        if task.parent_id and task.parent_id in by_id:
            neighbors[task.id].add(task.parent_id)
            neighbors[task.parent_id].add(task.id)
    hidden: set[str] = set()
    visited: set[str] = set()
    for task in tasks:
        if task.id in visited or task.state not in _HIDEABLE_STATES:
            continue
        # Flood fill the whole component from a finished seed.
        component: list[Task] = []
        stack = [task.id]
        all_finished = True
        while stack:
            node_id = stack.pop()
            if node_id in visited:
                continue
            visited.add(node_id)
            node = by_id[node_id]
            component.append(node)
            if node.state not in _HIDEABLE_STATES:
                all_finished = False
            stack.extend(neighbors[node_id] - visited)
        if all_finished:
            hidden.update(node.id for node in component)
    return hidden


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


SCHEDULE_FUTURE = "future"          # pending, start time ahead
SCHEDULE_OVERDUE = "overdue"        # pending, start time already passed
SCHEDULE_WAITING = "waiting_parent" # pending, chained after an unfinished parent


@dataclass(frozen=True)
class ScheduleInfo:
    """Display-only view of a task's pending unattended start."""

    status: str = ""             # "" = nothing pending to show
    at: datetime | None = None   # effective next start (local, naive)
    parent_id: str = ""
    parent_name: str = ""        # "" when the parent no longer exists


NO_SCHEDULE = ScheduleInfo()


def schedule_info(
    task: Task, by_id: dict[str, Task], now: datetime, *, running: bool = False,
) -> ScheduleInfo:
    """Pending-start status of ``task`` for the UI (never mutates it).

    Only DRAFT tasks that are not running can still start unattended:
    any other state (started, failed, paused, done, discarded) hides
    the schedule so a stale date is never shown. A DRAFT child whose
    parent is not DONE waits for the chain, with or without its own
    time; otherwise the effective ``next_start`` decides between
    future and overdue.
    """
    if running or task.state is not TaskState.DRAFT:
        return NO_SCHEDULE
    target = next_start(task)
    if task.parent_id and not parent_done(task, by_id):
        parent = by_id.get(task.parent_id)
        return ScheduleInfo(
            SCHEDULE_WAITING, target, task.parent_id, parent.name if parent else "",
        )
    if target is None:
        return NO_SCHEDULE
    return ScheduleInfo(SCHEDULE_FUTURE if target > now else SCHEDULE_OVERDUE, target)


def utc_offset_label(moment: datetime) -> str:
    """Local UTC offset of a naive local datetime, as ``UTC-05:00``."""
    raw = moment.astimezone().strftime("%z")  # "-0500"; naive = local time
    return f"UTC{raw[:3]}:{raw[3:]}" if raw else "UTC"


def _day_word(moment: datetime, now: datetime) -> str:
    """``today``/``tomorrow``/``yesterday`` by calendar date, else ``""``."""
    delta = (moment.date() - now.date()).days
    words = {0: "det.sched.today", 1: "det.sched.tomorrow", -1: "det.sched.yesterday"}
    return t(words[delta]) if delta in words else ""


def _when(moment: datetime, now: datetime, compact: bool) -> str:
    stamp = moment.strftime(SCHEDULE_FORMAT)
    day = _day_word(moment, now)
    if compact:
        return f"{stamp} ({day})" if day else stamp
    seconds = (moment - now).total_seconds()
    relative = (
        t("det.sched.in", d=format_countdown(seconds)) if seconds > 0
        else t("det.sched.ago", d=format_countdown(-seconds))
    )
    parts = ", ".join(part for part in (day, relative) if part)
    return f"{stamp} {utc_offset_label(moment)} ({parts})"


def format_schedule(info: ScheduleInfo, now: datetime, *, compact: bool = False) -> str:
    """Human text of a ``ScheduleInfo``; ``""`` when nothing is pending.

    ``compact`` (list cells) drops the UTC offset and the countdown: the
    list header carries the offset once.
    """
    if not info.status:
        return ""
    if info.status == SCHEDULE_WAITING:
        if not info.parent_name:
            return t("det.sched.parent_missing")
        if info.at is not None and info.at > now:
            return t("det.sched.waiting_parent_at", name=info.parent_name, at=_when(info.at, now, compact))
        return t("det.sched.waiting_parent", name=info.parent_name)
    if info.at is None:
        return ""
    when = _when(info.at, now, compact)
    if info.status == SCHEDULE_OVERDUE:
        return t("det.sched.overdue", at=when)
    return when if compact else t("det.scheduled", at=when)
