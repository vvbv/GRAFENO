"""Dated usage ledger: token/time records feeding the reports screen.

Task counters (``Task.tokens`` / ``Task.durations``) are lifetime
accumulators without dates, so they cannot answer "how much was used this
week". This module appends one dated record per pipeline recording to
``~/.grafeno/usage.toml`` (``[[record]]`` blocks, append-only). The first
touch of the ledger backfills the existing tasks, attributing each task's
accumulated totals to its ``updated_at`` day — best effort: precise per-day
data starts once the ledger exists.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import _toml, models, paths
from .models import DEFAULT_MODEL_LABEL, Task, cli_model_label


@dataclass
class UsageRecord:
    date: str        # local day "YYYY-MM-DD"
    task_id: str
    task_name: str
    project: str     # local workdir, or the canonical remote spec
    cli: str         # empty on duration-only records
    model: str       # empty on duration-only records
    phase: str
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: int = 0


def _project_label(task: Task) -> str:
    return task.remote if task.is_remote else task.workdir


def _today() -> str:
    return date.today().isoformat()


def _task_day(task: Task) -> str:
    """Best-effort day for backfilled records: updated_at, created_at or today."""
    for stamp in (task.updated_at, task.created_at):
        day = stamp[:10]
        try:
            date.fromisoformat(day)
            return day
        except ValueError:
            continue
    return _today()


# ------------------------------------------------------------------ write #
def record_tokens(
    task: Task, cli: str, model: str, phase: str, tokens_in: int, tokens_out: int
) -> None:
    """Append a dated token record. Never raises: the pipeline must not break."""
    if tokens_in <= 0 and tokens_out <= 0:
        return
    _append(UsageRecord(
        date=_today(),
        task_id=task.id,
        task_name=task.name,
        project=_project_label(task),
        cli=cli,
        model=model or DEFAULT_MODEL_LABEL,
        phase=phase,
        tokens_in=max(0, tokens_in),
        tokens_out=max(0, tokens_out),
    ))


def record_duration(task: Task, phase: str, seconds: int) -> None:
    """Append a dated duration record. Never raises: the pipeline must not break."""
    if seconds <= 0:
        return
    _append(UsageRecord(
        date=_today(),
        task_id=task.id,
        task_name=task.name,
        project=_project_label(task),
        cli="",
        model="",
        phase=phase,
        seconds=seconds,
    ))


def _record_block(record: UsageRecord) -> str:
    """One appendable ``[[record]]`` block (no file header, no leading blank)."""
    dump = _toml.dumps({"record": [asdict(record)]})
    lines = [line for line in dump.splitlines() if line and not line.startswith("#")]
    return "\n".join(lines) + "\n\n"


def _append(record: UsageRecord) -> None:
    try:
        ensure_backfill()
        with paths.usage_path().open("a", encoding="utf-8") as handle:
            handle.write(_record_block(record))
    except OSError:
        pass  # a broken ledger never breaks the pipeline


def ensure_backfill() -> None:
    """Create the ledger on first touch, seeded from the existing tasks.

    The file's existence is the marker: it is written even with no tasks, so
    the backfill runs exactly once. Callers recording a new run must call
    this BEFORE mutating the task counters, or the run would be counted
    twice (once in the backfilled totals, once as its own record).
    """
    path = paths.usage_path()
    if path.exists():
        return
    records: list[dict] = []
    for task in models.list_all():
        day = _task_day(task)
        by_run: dict[tuple[str, str, str], list[int]] = {}
        for key, value in task.tokens.items():
            phase, cli, model, kind = models._parse_token_key(key)
            entry = by_run.setdefault((phase, cli, model), [0, 0])
            if kind == "input":
                entry[0] += value
            elif kind == "output":
                entry[1] += value
        for (phase, cli, model), (tokens_in, tokens_out) in sorted(by_run.items()):
            if tokens_in <= 0 and tokens_out <= 0:
                continue
            records.append(asdict(UsageRecord(
                date=day, task_id=task.id, task_name=task.name,
                project=_project_label(task), cli=cli, model=model,
                phase=phase, tokens_in=tokens_in, tokens_out=tokens_out,
            )))
        for phase, seconds in sorted(task.durations.items()):
            if seconds <= 0:
                continue
            records.append(asdict(UsageRecord(
                date=day, task_id=task.id, task_name=task.name,
                project=_project_label(task), cli="", model="",
                phase=phase, seconds=int(seconds),
            )))
    path.parent.mkdir(parents=True, exist_ok=True)
    # No records: dumps({}) still writes the header comment, marking the
    # backfill as done ("record = []" would clash with appended [[record]]).
    content = _toml.dumps({"record": records} if records else {})
    path.write_text(content, encoding="utf-8")


# ------------------------------------------------------------------- read #
def load_records() -> list[UsageRecord]:
    """Every ledger record; the first read backfills the existing tasks."""
    path = paths.usage_path()
    try:
        ensure_backfill()
        raw_text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = tomllib.loads(raw_text)
    except tomllib.TOMLDecodeError:
        return []
    records: list[UsageRecord] = []
    for raw in data.get("record", []):
        if not isinstance(raw, dict):
            continue
        try:
            records.append(UsageRecord(
                date=str(raw.get("date", "")),
                task_id=str(raw.get("task_id", "")),
                task_name=str(raw.get("task_name", "")),
                project=str(raw.get("project", "")),
                cli=str(raw.get("cli", "")),
                model=str(raw.get("model", "")),
                phase=str(raw.get("phase", "")),
                tokens_in=int(raw.get("tokens_in", 0)),
                tokens_out=int(raw.get("tokens_out", 0)),
                seconds=int(raw.get("seconds", 0)),
            ))
        except (TypeError, ValueError):
            continue  # corrupt record: ignored
    return records


def records_between(
    records: list[UsageRecord], start: date, end: date
) -> list[UsageRecord]:
    """Records whose day falls within ``[start, end]`` (both inclusive)."""
    lo, hi = start.isoformat(), end.isoformat()
    return [record for record in records if lo <= record.date <= hi]


# ---------------------------------------------------------------- periods #
def period_day(day: date) -> tuple[date, date]:
    return day, day


def period_week(day: date) -> tuple[date, date]:
    """Monday..Sunday of the week containing ``day``."""
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def period_month(day: date) -> tuple[date, date]:
    """First..last day of the month containing ``day``."""
    first = day.replace(day=1)
    if first.month == 12:
        next_first = first.replace(year=first.year + 1, month=1)
    else:
        next_first = first.replace(month=first.month + 1)
    return first, next_first - timedelta(days=1)


# -------------------------------------------------------------- aggregate #
@dataclass
class UsageSummary:
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: int = 0
    task_ids: set[str] = field(default_factory=set)
    by_day: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    by_model: dict[str, tuple[int, int]] = field(default_factory=dict)
    by_project: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)


def summarize(records: list[UsageRecord]) -> UsageSummary:
    """Aggregate records: totals plus day / cli+model / project breakdowns.

    ``by_day``: date -> (in, out, seconds). ``by_model``: "cli/model" ->
    (in, out) — duration records carry no model, so time is not attributable
    per model. ``by_project``: project -> (in, out, seconds, task count).
    """
    summary = UsageSummary()
    days: dict[str, list[int]] = {}
    labels: dict[str, list[int]] = {}
    projects: dict[str, list[int]] = {}
    project_tasks: dict[str, set[str]] = {}
    for record in records:
        summary.tokens_in += record.tokens_in
        summary.tokens_out += record.tokens_out
        summary.seconds += record.seconds
        if record.task_id:
            summary.task_ids.add(record.task_id)
        day = days.setdefault(record.date, [0, 0, 0])
        day[0] += record.tokens_in
        day[1] += record.tokens_out
        day[2] += record.seconds
        if record.cli or record.model:
            label = labels.setdefault(cli_model_label(record.cli, record.model), [0, 0])
            label[0] += record.tokens_in
            label[1] += record.tokens_out
        project = projects.setdefault(record.project, [0, 0, 0])
        project[0] += record.tokens_in
        project[1] += record.tokens_out
        project[2] += record.seconds
        if record.task_id:
            project_tasks.setdefault(record.project, set()).add(record.task_id)
    summary.by_day = {day: (v[0], v[1], v[2]) for day, v in days.items()}
    summary.by_model = {label: (v[0], v[1]) for label, v in labels.items()}
    summary.by_project = {
        project: (v[0], v[1], v[2], len(project_tasks.get(project, set())))
        for project, v in projects.items()
    }
    return summary
