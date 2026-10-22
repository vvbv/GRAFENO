"""Current server date and time for API clients (``GET /api/v1/time``)."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .. import scheduler

# Fixed English names: strftime("%A") depends on the process locale.
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

LOCALTIME_PATH = Path("/etc/localtime")
TIMEZONE_FILE = Path("/etc/timezone")
_ZONEINFO_PREFIXES = ("posix/", "right/")  # alternate tzdata trees under zoneinfo/


def _zone_matches(name: str, moment: datetime) -> bool:
    """True if ``name`` is a known IANA zone whose offset at ``moment`` matches."""
    if not name:
        return False
    try:
        zone = ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return moment.astimezone(zone).utcoffset() == moment.utcoffset()


def _name_from_localtime(path: Path) -> str:
    """IANA name from the ``/etc/localtime`` symlink target, else ``""``."""
    try:
        target = path.resolve().as_posix()
    except OSError:
        return ""
    _, sep, name = target.rpartition("/zoneinfo/")
    if not sep:
        return ""
    for prefix in _ZONEINFO_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def _name_from_file(path: Path) -> str:
    """First line of ``/etc/timezone`` (Debian), else ``""``."""
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


def local_timezone_name(
    moment: datetime,
    *,
    localtime_path: Path = LOCALTIME_PATH,
    timezone_file: Path = TIMEZONE_FILE,
) -> str:
    """Best-effort IANA name of the host timezone (``America/Bogota``).

    Sources in order: ``TZ`` env, ``/etc/localtime`` symlink, ``/etc/timezone``.
    A candidate is only returned when its offset at ``moment`` (aware) matches;
    ``""`` when nothing reliable is found (never a wrong name).
    """
    env = os.environ.get("TZ", "").strip().lstrip(":")
    if env:
        # TZ overrides the system files: if it is not an IANA name (POSIX
        # string such as "EST5EDT,M3.2.0,M11.1.0"), the files do not apply.
        return env if _zone_matches(env, moment) else ""
    for candidate in (_name_from_localtime(localtime_path), _name_from_file(timezone_file)):
        if _zone_matches(candidate, moment):
            return candidate
    return ""


def server_time(now: datetime | None = None, *, zone: str | None = None) -> dict:
    """Payload of the current server time; ``now``/``zone`` are injectable for tests.

    A naive ``now`` is host local time; an aware one keeps its offset.
    ``zone=None`` resolves the IANA name with ``local_timezone_name``.
    """
    base = now if now is not None else datetime.now()
    moment = base if base.tzinfo is not None else base.astimezone()
    offset = moment.utcoffset()
    offset_minutes = offset // timedelta(minutes=1) if offset is not None else 0
    stamp = moment.timestamp()
    return {
        "iso": moment.isoformat(timespec="seconds"),
        "utc": moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "epoch": int(stamp),
        "epoch_ms": int(stamp * 1000),
        "date": moment.date().isoformat(),
        "time": moment.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS[moment.weekday()],
        "iso_weekday": moment.isoweekday(),
        "timezone": local_timezone_name(moment) if zone is None else zone,
        "tz_abbreviation": moment.tzname() or "",
        "utc_offset": scheduler.utc_offset_label(moment),
        "utc_offset_minutes": offset_minutes,
    }
