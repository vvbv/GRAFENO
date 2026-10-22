"""Tests of the server clock payload (GET /api/v1/time, WS ``time``)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from grafeno.server import clock

BOGOTA = timezone(timedelta(hours=-5))
KEYS = {
    "iso", "utc", "epoch", "epoch_ms", "date", "time", "weekday", "iso_weekday",
    "timezone", "tz_abbreviation", "utc_offset", "utc_offset_minutes",
}


def _require_zone(name: str) -> None:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        pytest.skip(f"tzdata without {name}")


def test_server_time_fixed_moment() -> None:
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    payload = clock.server_time(moment, zone="America/Bogota")
    assert set(payload) == KEYS
    assert payload["iso"] == "2026-10-21T00:51:06-05:00"
    assert payload["utc"] == "2026-10-21T05:51:06Z"
    assert payload["epoch"] == int(moment.timestamp())
    assert payload["epoch_ms"] == payload["epoch"] * 1000
    assert payload["date"] == "2026-10-21"
    assert payload["time"] == "00:51:06"
    assert payload["weekday"] == "Wednesday"
    assert payload["iso_weekday"] == 3
    assert payload["timezone"] == "America/Bogota"
    assert payload["utc_offset"] == "UTC-05:00"
    assert payload["utc_offset_minutes"] == -300


def test_server_time_half_hour_offset() -> None:
    moment = datetime(2026, 10, 21, 8, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    payload = clock.server_time(moment, zone="")
    assert payload["utc_offset"] == "UTC+05:30"
    assert payload["utc_offset_minutes"] == 330
    assert payload["iso"].endswith("+05:30")


def test_server_time_utc_uses_z_suffix() -> None:
    payload = clock.server_time(datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc), zone="")
    assert payload["utc"].endswith("Z")
    assert payload["utc_offset"] == "UTC+00:00"
    assert payload["utc_offset_minutes"] == 0


def test_server_time_date_changes_across_utc() -> None:
    payload = clock.server_time(datetime(2026, 10, 21, 23, 30, 0, tzinfo=BOGOTA), zone="")
    assert payload["date"] == "2026-10-21"
    assert payload["utc"].startswith("2026-10-22T04:30:00")


def test_server_time_default_is_now() -> None:
    payload = clock.server_time(zone="")
    iso = datetime.fromisoformat(payload["iso"])
    assert iso.tzinfo is not None
    assert abs(payload["epoch"] - time.time()) < 5
    assert datetime.fromisoformat(payload["utc"].replace("Z", "+00:00")) == iso


def test_server_time_naive_is_local() -> None:
    naive = datetime(2026, 10, 21, 12, 0, 0)
    payload = clock.server_time(naive, zone="")
    assert payload["iso"].startswith("2026-10-21T12:00:00")
    assert payload["epoch"] == int(naive.timestamp())


def test_local_timezone_name_from_tz_env(monkeypatch) -> None:
    _require_zone("America/Bogota")
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    monkeypatch.setenv("TZ", "America/Bogota")
    assert clock.local_timezone_name(moment) == "America/Bogota"
    monkeypatch.setenv("TZ", ":America/Bogota")
    assert clock.local_timezone_name(moment) == "America/Bogota"


def test_local_timezone_name_rejects_mismatch_and_unknown(monkeypatch) -> None:
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    for value in ("Asia/Tokyo", "Nope/Zone", "EST5EDT,M3.2.0,M11.1.0"):
        monkeypatch.setenv("TZ", value)
        assert clock.local_timezone_name(moment) == ""


def test_local_timezone_name_from_localtime_symlink(tmp_path, monkeypatch) -> None:
    _require_zone("America/Bogota")
    monkeypatch.delenv("TZ", raising=False)
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    for sub in ("zoneinfo/America/Bogota", "zoneinfo/posix/America/Bogota"):
        target = tmp_path / sub
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")
        link = tmp_path / ("localtime-" + sub.replace("/", "_"))
        link.symlink_to(target)
        name = clock.local_timezone_name(
            moment, localtime_path=link, timezone_file=tmp_path / "missing",
        )
        assert name == "America/Bogota"


def test_local_timezone_name_from_timezone_file(tmp_path, monkeypatch) -> None:
    _require_zone("America/Bogota")
    monkeypatch.delenv("TZ", raising=False)
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    tzfile = tmp_path / "timezone"
    tzfile.write_text("America/Bogota\n")
    assert clock.local_timezone_name(
        moment, localtime_path=tmp_path / "missing", timezone_file=tzfile,
    ) == "America/Bogota"


def test_local_timezone_name_nothing_found(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    moment = datetime(2026, 10, 21, 0, 51, 6, tzinfo=BOGOTA)
    assert clock.local_timezone_name(
        moment, localtime_path=tmp_path / "a", timezone_file=tmp_path / "b",
    ) == ""
