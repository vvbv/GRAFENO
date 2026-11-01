"""Usage reports screen: tokens, models, time and projects per period."""

from __future__ import annotations

from datetime import date, datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from ... import quota, usage
from ...i18n import t
from ...timefmt import format_countdown, format_duration
from ...tokenfmt import format_tokens
from ..widgets import GrafenoHeader, LocationBar


class ReportsScreen(Screen[None]):
    """Usage aggregated by day, CLI+model and project over a date period."""

    BINDINGS = [
        Binding("escape", "back", t("common.back")),
        Binding("r", "reload", t("tasks.bind.reload")),
        Binding("u", "refresh_quota", t("reports.quota.refresh")),
    ]

    def __init__(self) -> None:
        super().__init__()
        today = date.today()
        self._start = today
        self._end = today

    def compose(self) -> ComposeResult:
        yield GrafenoHeader()
        yield LocationBar(id="location-bar")
        yield Static(t("reports.subtitle"), id="subtitle")
        with Horizontal(id="reports-period"):
            yield Button(t("reports.period.today"), id="rp-today", compact=True)
            yield Button(t("reports.period.week"), id="rp-week", compact=True)
            yield Button(t("reports.period.month"), id="rp-month", compact=True)
            yield Input(placeholder=t("reports.from.placeholder"), id="rp-from")
            yield Input(placeholder=t("reports.to.placeholder"), id="rp-to")
            yield Button(t("reports.period.apply"), id="rp-apply", compact=True)
        yield Static("", id="reports-range")
        yield Static("", id="reports-summary")
        with VerticalScroll(id="reports-body"):
            yield Label(t("reports.quota.title"), classes="report-section")
            yield Static(
                t("reports.quota.subtitle", minutes=quota.REFRESH_SECONDS // 60),
                id="quota-subtitle",
            )
            yield Static(t("reports.quota.loading"), id="quota-info")
            yield DataTable(
                id="report-quota", classes="report-table",
                cursor_type="none", zebra_stripes=True,
            )
            yield Label(t("reports.section.by_day"), classes="report-section")
            yield DataTable(
                id="report-by-day", classes="report-table",
                cursor_type="none", zebra_stripes=True,
            )
            yield Label(t("reports.section.by_model"), classes="report-section")
            yield DataTable(
                id="report-by-model", classes="report-table",
                cursor_type="none", zebra_stripes=True,
            )
            yield Label(t("reports.section.by_project"), classes="report-section")
            yield DataTable(
                id="report-by-project", classes="report-table",
                cursor_type="none", zebra_stripes=True,
            )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#report-by-day", DataTable).add_columns(
            t("reports.col.date"),
            t("reports.col.tokens_in"),
            t("reports.col.tokens_out"),
            t("reports.col.time"),
        )
        self.query_one("#report-by-model", DataTable).add_columns(
            t("reports.col.model"),
            t("reports.col.tokens_in"),
            t("reports.col.tokens_out"),
        )
        self.query_one("#report-by-project", DataTable).add_columns(
            t("reports.col.project"),
            t("reports.col.tokens_in"),
            t("reports.col.tokens_out"),
            t("reports.col.time"),
            t("reports.col.tasks"),
        )
        self.query_one("#report-quota", DataTable).add_columns(
            t("reports.quota.col.cli"),
            t("reports.quota.col.window"),
            t("reports.quota.col.used"),
            t("reports.quota.col.remaining"),
            t("reports.quota.col.resets"),
            t("reports.quota.col.status"),
            t("reports.quota.col.used_by"),
        )
        self._set_period(*usage.period_day(date.today()))
        self._start_quota(live=False)
        self.set_interval(quota.REFRESH_SECONDS, lambda: self._start_quota(live=False))

    # ------------------------------------------------------------ period #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        today = date.today()
        if event.button.id == "rp-today":
            self._set_period(*usage.period_day(today))
        elif event.button.id == "rp-week":
            self._set_period(*usage.period_week(today))
        elif event.button.id == "rp-month":
            self._set_period(*usage.period_month(today))
        elif event.button.id == "rp-apply":
            self._apply_inputs()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id in ("rp-from", "rp-to"):
            self._apply_inputs()

    def _apply_inputs(self) -> None:
        """Period from the inputs: one date = that single day; two = range."""
        raw_from = self.query_one("#rp-from", Input).value.strip()
        raw_to = self.query_one("#rp-to", Input).value.strip()
        try:
            start = date.fromisoformat(raw_from) if raw_from else None
            end = date.fromisoformat(raw_to) if raw_to else None
        except ValueError:
            self.notify(t("reports.error.bad_date"), severity="error")
            return
        if start is None and end is None:
            self.notify(t("reports.error.bad_date"), severity="error")
            return
        if start is None:
            start = end
        if end is None:
            end = start
        if start > end:
            start, end = end, start
        self._set_period(start, end)

    def _set_period(self, start: date, end: date) -> None:
        self._start, self._end = start, end
        # Reflected in the inputs so the active period is always visible.
        self.query_one("#rp-from", Input).value = start.isoformat()
        self.query_one("#rp-to", Input).value = end.isoformat()
        self._reload()

    # ------------------------------------------------------------ render #
    def action_back(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._reload()

    def _reload(self) -> None:
        records = usage.records_between(usage.load_records(), self._start, self._end)
        summary = usage.summarize(records)
        if self._start == self._end:
            range_text = t("reports.range.day", day=self._start.isoformat())
        else:
            range_text = t(
                "reports.range",
                start=self._start.isoformat(), end=self._end.isoformat(),
            )
        self.query_one("#reports-range", Static).update(range_text)
        summary_widget = self.query_one("#reports-summary", Static)
        if not records:
            summary_widget.update(t("reports.empty"))
        else:
            summary_widget.update(t(
                "reports.summary",
                tokens_in=format_tokens(summary.tokens_in),
                tokens_out=format_tokens(summary.tokens_out),
                duration=format_duration(summary.seconds),
                tasks=len(summary.task_ids),
                projects=len(summary.by_project),
            ))

        by_day = self.query_one("#report-by-day", DataTable)
        by_day.clear()
        for day in sorted(summary.by_day):
            tokens_in, tokens_out, seconds = summary.by_day[day]
            by_day.add_row(
                day,
                format_tokens(tokens_in),
                format_tokens(tokens_out),
                format_duration(seconds) if seconds else "",
            )

        by_model = self.query_one("#report-by-model", DataTable)
        by_model.clear()
        for label, (tokens_in, tokens_out) in sorted(
            summary.by_model.items(),
            key=lambda item: (-(item[1][0] + item[1][1]), item[0]),
        ):
            by_model.add_row(label, format_tokens(tokens_in), format_tokens(tokens_out))

        by_project = self.query_one("#report-by-project", DataTable)
        by_project.clear()
        for project, (tokens_in, tokens_out, seconds, tasks) in sorted(
            summary.by_project.items(),
            key=lambda item: (-(item[1][0] + item[1][1]), item[0]),
        ):
            by_project.add_row(
                project,
                format_tokens(tokens_in),
                format_tokens(tokens_out),
                format_duration(seconds) if seconds else "",
                str(tasks),
            )

    # ------------------------------------------------------------- quota #
    def action_refresh_quota(self) -> None:
        self._start_quota(live=True)

    def _monitor(self) -> quota.QuotaMonitor:
        found = getattr(self.app, "quota_monitor", None)
        if isinstance(found, quota.QuotaMonitor):
            return found
        self._own_monitor = getattr(self, "_own_monitor", None) or quota.QuotaMonitor()
        return self._own_monitor

    def _start_quota(self, live: bool) -> None:
        self.run_worker(self._load_quota(live), group="quota", exclusive=True, exit_on_error=False)

    async def _load_quota(self, live: bool) -> None:
        payload = await self._monitor().get(live=live)
        self._render_quota(payload)

    @staticmethod
    def _quota_rows(cli: dict) -> list[tuple]:
        """Table rows of one serialized CliQuota (one per window, or one status row)."""
        status = quota.status_label(str(cli.get("status", "")))
        if cli.get("stale"):
            status += f" ({t('reports.quota.stale')})"
        if cli.get("source") == "stream":
            status += f" ({t('reports.quota.source.stream')})"
        used_by = quota.used_by_label(cli)
        windows = cli.get("windows") or []
        if not windows:
            message = str(cli.get("message") or "")[:80]
            if message:
                status += f" ({message})"
            return [(cli.get("cli", ""), "", "", "", "", status, used_by)]
        rows: list[tuple] = []
        for index, window in enumerate(windows):
            used = float(window.get("used_percent", 0))
            style = "red" if used >= 95 else "yellow" if used >= 80 else ""
            seconds = window.get("resets_in_seconds")
            resets = (
                t("reports.quota.resets_in", time=format_countdown(seconds))
                if seconds is not None else ""
            )
            rows.append((
                cli.get("cli", "") if index == 0 else "",
                quota.window_label(str(window.get("key", ""))),
                Text(f"{used:.0f}%", style=style),
                f"{float(window.get('remaining_percent', 0)):.0f}%",
                resets,
                status,
                used_by,
            ))
        return rows

    def _render_quota(self, payload: dict) -> None:
        if not self.is_mounted:
            return
        clis = payload.get("clis") or []
        fetched = self._time_of(str(payload.get("fetched_at", "")))
        info = t("reports.quota.fetched_at", time=fetched) if fetched else ""
        if not clis:
            info = t("reports.quota.empty")
        if payload.get("throttled"):
            info += " - " + t("reports.quota.throttled", seconds=payload.get("retry_after", 0))
        self.query_one("#quota-info", Static).update(info)
        table = self.query_one("#report-quota", DataTable)
        table.clear()
        for cli in clis:
            for row in self._quota_rows(cli):
                table.add_row(*row)

    @staticmethod
    def _time_of(iso: str) -> str:
        try:
            return datetime.fromisoformat(iso).astimezone().strftime("%H:%M:%S")
        except ValueError:
            return ""
