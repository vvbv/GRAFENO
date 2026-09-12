"""Tests of the dated usage ledger (record, backfill, periods, aggregation)."""

from __future__ import annotations

import os
from datetime import date

from grafeno import _toml, models, paths, usage
from grafeno.config import Config
from grafeno.models import Task


def _make_task(name: str = "Tarea") -> Task:
    task = Task.create(name, "desc", os.getcwd(), Config())
    models.save(task)
    return task


def test_record_tokens_and_duration_round_trip():
    task = _make_task()
    usage.record_tokens(task, "opencode", "gpt-x", "plan", 100, 20)
    usage.record_duration(task, "plan", 42)

    records = usage.load_records()
    assert len(records) == 2
    tokens = records[0]
    assert tokens.date == date.today().isoformat()
    assert tokens.task_id == task.id
    assert tokens.project == task.workdir
    assert (tokens.cli, tokens.model, tokens.phase) == ("opencode", "gpt-x", "plan")
    assert (tokens.tokens_in, tokens.tokens_out, tokens.seconds) == (100, 20, 0)
    duration = records[1]
    assert (duration.cli, duration.model) == ("", "")
    assert duration.seconds == 42


def test_record_skips_empty_amounts():
    task = _make_task()
    usage.record_tokens(task, "opencode", "gpt-x", "plan", 0, 0)
    usage.record_duration(task, "plan", 0)
    assert usage.load_records() == []


def test_default_model_label_applied():
    task = _make_task()
    usage.record_tokens(task, "opencode", "", "plan", 10, 1)
    assert usage.load_records()[0].model == "default"


def test_backfill_dates_existing_totals_to_updated_at():
    task = _make_task()
    task.tokens["plan|opencode|gpt-x|input"] = 500
    task.tokens["plan|opencode|gpt-x|output"] = 50
    task.durations["plan"] = 120
    task.updated_at = "2026-09-01T10:00:00"
    # Write without models.save(): save refreshes updated_at to today.
    paths.task_meta_path(task.id).write_text(
        _toml.dumps(task.to_dict()), encoding="utf-8"
    )

    records = usage.load_records()  # first read triggers the backfill
    assert paths.usage_path().exists()
    assert len(records) == 2
    assert all(record.date == "2026-09-01" for record in records)
    tokens = [r for r in records if r.tokens_in][0]
    assert (tokens.tokens_in, tokens.tokens_out) == (500, 50)
    assert [r for r in records if r.seconds][0].seconds == 120

    # The backfill runs exactly once: a reload does not duplicate records.
    assert len(usage.load_records()) == 2


def test_backfill_with_no_tasks_marks_done_and_appends_work():
    task = _make_task()
    assert usage.load_records() == []  # backfilled empty (task has no usage)
    assert paths.usage_path().exists()
    usage.record_tokens(task, "kimi", "k2", "implement", 7, 3)
    records = usage.load_records()
    assert len(records) == 1
    assert records[0].cli == "kimi"


def test_records_between_inclusive():
    records = [
        usage.UsageRecord(date=d, task_id="t", task_name="t", project="p",
                          cli="c", model="m", phase="plan", tokens_in=1)
        for d in ("2026-09-01", "2026-09-05", "2026-09-10")
    ]
    picked = usage.records_between(records, date(2026, 9, 1), date(2026, 9, 5))
    assert [r.date for r in picked] == ["2026-09-01", "2026-09-05"]


def test_period_helpers():
    day = date(2026, 9, 12)  # Saturday
    assert usage.period_day(day) == (day, day)
    assert usage.period_week(day) == (date(2026, 9, 7), date(2026, 9, 13))
    assert usage.period_month(day) == (date(2026, 9, 1), date(2026, 9, 30))
    assert usage.period_month(date(2026, 12, 15)) == (
        date(2026, 12, 1), date(2026, 12, 31)
    )


def test_summarize_breakdowns():
    def rec(**kwargs):
        base = dict(date="2026-09-12", task_id="t1", task_name="n", project="/p1",
                    cli="opencode", model="gpt-x", phase="plan")
        base.update(kwargs)
        return usage.UsageRecord(**base)

    records = [
        rec(tokens_in=100, tokens_out=10),
        rec(date="2026-09-11", cli="kimi", model="k2", task_id="t2",
            project="/p2", tokens_in=30, tokens_out=3),
        rec(cli="", model="", seconds=60),  # duration: no model attribution
    ]
    summary = usage.summarize(records)
    assert (summary.tokens_in, summary.tokens_out, summary.seconds) == (130, 13, 60)
    assert summary.task_ids == {"t1", "t2"}
    assert summary.by_day["2026-09-12"] == (100, 10, 60)
    assert summary.by_day["2026-09-11"] == (30, 3, 0)
    assert summary.by_model == {"opencode/gpt-x": (100, 10), "kimi/k2": (30, 3)}
    assert summary.by_project["/p1"] == (100, 10, 60, 1)
    assert summary.by_project["/p2"] == (30, 3, 0, 1)


def test_corrupt_ledger_returns_empty():
    paths.usage_path().parent.mkdir(parents=True, exist_ok=True)
    paths.usage_path().write_text("esto no es TOML [[[", encoding="utf-8")
    assert usage.load_records() == []
