"""Tests of the usage reports screen (periods, tables, navigation)."""

from __future__ import annotations

import asyncio
import os
from datetime import date

from textual.widgets import DataTable, Input, Static

from grafeno import models, usage
from grafeno.app import GrafenoApp
from grafeno.config import Config
from grafeno.models import Task
from grafeno.tui.screens.reports import ReportsScreen
from grafeno.tui.screens.tasks import TaskListScreen


def _task_with_usage() -> Task:
    task = Task.create("Con uso", "desc", os.getcwd(), Config())
    models.save(task)
    usage.record_tokens(task, "opencode", "gpt-x", "plan", 1200, 300)
    usage.record_duration(task, "plan", 90)
    return task


def _rows(table: DataTable) -> list[list[str]]:
    return [
        [str(cell) for cell in table.get_row_at(index)]
        for index in range(table.row_count)
    ]


def test_reports_screen_opens_with_today_and_shows_usage():
    task = _task_with_usage()

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ReportsScreen)

            today = date.today().isoformat()
            assert screen.query_one("#rp-from", Input).value == today
            assert screen.query_one("#rp-to", Input).value == today

            by_day = _rows(screen.query_one("#report-by-day", DataTable))
            assert len(by_day) == 1
            assert by_day[0][0] == today
            assert by_day[0][1] == "1.2k"

            by_model = _rows(screen.query_one("#report-by-model", DataTable))
            assert by_model == [["opencode/gpt-x", "1.2k", "300"]]

            by_project = _rows(screen.query_one("#report-by-project", DataTable))
            assert len(by_project) == 1
            assert by_project[0][0] == task.workdir
            assert by_project[0][3] == "1m 30s"
            assert by_project[0][4] == "1"

            summary = str(screen.query_one("#reports-summary", Static).render())
            assert "1.2k" in summary

            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, TaskListScreen)

    asyncio.run(scenario())


def test_reports_screen_week_month_and_range():
    _task_with_usage()
    # An old record outside every current period must be excluded.
    usage._append(usage.UsageRecord(
        date="2020-01-15", task_id="viejo", task_name="Viejo", project="/old",
        cli="opencode", model="gpt-x", phase="plan", tokens_in=999,
    ))

    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            await pilot.press("i")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ReportsScreen)

            await pilot.click("#rp-week")
            await pilot.pause()
            start, end = usage.period_week(date.today())
            assert screen.query_one("#rp-from", Input).value == start.isoformat()
            assert screen.query_one("#rp-to", Input).value == end.isoformat()
            projects = [row[0] for row in _rows(screen.query_one("#report-by-project", DataTable))]
            assert "/old" not in projects

            await pilot.click("#rp-month")
            await pilot.pause()
            start, end = usage.period_month(date.today())
            assert screen.query_one("#rp-from", Input).value == start.isoformat()
            assert screen.query_one("#rp-to", Input).value == end.isoformat()

            # Explicit range covering the old record.
            screen.query_one("#rp-from", Input).value = "2020-01-01"
            screen.query_one("#rp-to", Input).value = "2020-01-31"
            await pilot.click("#rp-apply")
            await pilot.pause()
            projects = [row[0] for row in _rows(screen.query_one("#report-by-project", DataTable))]
            assert projects == ["/old"]

            # A single date reports just that day. The pause waits out the
            # button's 0.2s active effect: Textual swallows a re-click on a
            # still-active button.
            await pilot.pause(0.3)
            screen.query_one("#rp-from", Input).value = "2020-01-15"
            screen.query_one("#rp-to", Input).value = ""
            await pilot.click("#rp-apply")
            await pilot.pause()
            assert screen.query_one("#rp-to", Input).value == "2020-01-15"
            by_day = _rows(screen.query_one("#report-by-day", DataTable))
            assert [row[0] for row in by_day] == ["2020-01-15"]

            # An invalid date keeps the current period.
            await pilot.pause(0.3)  # wait out the active effect again
            screen.query_one("#rp-from", Input).value = "no-es-fecha"
            await pilot.click("#rp-apply")
            await pilot.pause()
            assert screen._start == date(2020, 1, 15)

    asyncio.run(scenario())
