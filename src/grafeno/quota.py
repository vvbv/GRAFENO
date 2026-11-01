"""Available usage (quota windows) of the agent CLIs used by the config and the profiles."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from . import config as config_mod
from . import profiles as profiles_mod
from .config import KNOWN_CLIS, Config
from .drivers import get_driver
from .modelcheck import role_items
from .profiles import Profile
from .i18n import t
from .telegram.api import USER_AGENT, Opener, default_opener

REFRESH_SECONDS = 300        # cached snapshot lifetime; views poll at this pace
LIVE_MIN_INTERVAL = 60       # minimum seconds between two live fetches (throttling)
JUST_FETCHED_SECONDS = 2.0   # a refresh this recent is reused by a waiting caller
FETCH_TIMEOUT = 10.0         # per-provider HTTP timeout
CODEX_TAIL_BYTES = 256 * 1024
STATUS_OK = "ok"
STATUS_UNSUPPORTED = "unsupported"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_NO_CREDENTIALS = "no_credentials"
STATUS_AUTH_EXPIRED = "auth_expired"
STATUS_NO_DATA = "no_data"
STATUS_ERROR = "error"
STATUSES = (STATUS_OK, STATUS_UNSUPPORTED, STATUS_NOT_INSTALLED, STATUS_NO_CREDENTIALS,
            STATUS_AUTH_EXPIRED, STATUS_NO_DATA, STATUS_ERROR)
WINDOW_KEYS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "extra_usage",
               "month_total", "month_code", "primary", "secondary")

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KIMI_DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"


# ------------------------------------------------------------------ helpers
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso_from_epoch(seconds: float) -> str:
    return datetime.fromtimestamp(float(seconds)).astimezone().isoformat(timespec="seconds")


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO 8601 string (``Z`` accepted) into an aware datetime."""
    text = (value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _clamp_percent(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return max(0.0, min(100.0, number))


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def _normalize_iso(value: Any) -> str:
    parsed = _parse_iso(str(value or ""))
    return parsed.isoformat(timespec="seconds") if parsed else ""


# --------------------------------------------------------------- data model
@dataclass
class QuotaWindow:
    """One usage window (e.g. the 5-hour or the weekly limit)."""

    key: str                 # one of WINDOW_KEYS
    used_percent: float      # 0-100, clamped
    resets_at: str = ""      # ISO 8601 with offset; "" = unknown

    def to_dict(self, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        used = _clamp_percent(self.used_percent)
        reset = _parse_iso(self.resets_at)
        in_seconds = max(0, int((reset - now).total_seconds())) if reset else None
        return {
            "key": self.key,
            "used_percent": round(used, 1),
            "remaining_percent": round(max(0.0, 100.0 - used), 1),
            "resets_at": self.resets_at,
            "resets_in_seconds": in_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QuotaWindow":
        return cls(
            key=str(data.get("key", "")),
            used_percent=_clamp_percent(data.get("used_percent", 0)),
            resets_at=str(data.get("resets_at", "") or ""),
        )


@dataclass
class CliQuota:
    """Quota state of one CLI."""

    cli: str
    status: str = STATUS_NO_DATA
    source: str = ""         # "api" | "stream" | "rollout" | ""
    windows: list[QuotaWindow] = field(default_factory=list)
    message: str = ""        # short technical detail (English), never a credential
    fetched_at: str = ""     # ISO 8601 of the data (not of the request)
    stale: bool = False      # windows come from an earlier successful fetch
    installed: bool = True
    used_by_default: bool = False
    profiles: list[str] = field(default_factory=list)

    def to_dict(self, now: datetime | None = None) -> dict:
        now = now or datetime.now().astimezone()
        return {
            "cli": self.cli,
            "status": self.status,
            "source": self.source,
            "windows": [w.to_dict(now) for w in self.windows],
            "message": self.message,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
            "installed": self.installed,
            "used_by_default": self.used_by_default,
            "profiles": list(self.profiles),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CliQuota":
        return cls(
            cli=str(data.get("cli", "")),
            status=str(data.get("status", STATUS_NO_DATA)),
            source=str(data.get("source", "")),
            windows=[QuotaWindow.from_dict(w) for w in data.get("windows", []) or []],
            message=str(data.get("message", "")),
            fetched_at=str(data.get("fetched_at", "")),
            stale=bool(data.get("stale", False)),
            installed=bool(data.get("installed", True)),
            used_by_default=bool(data.get("used_by_default", False)),
            profiles=[str(p) for p in data.get("profiles", []) or []],
        )


# ------------------------------------------------------------ participants
def configured_clis(cfg: Config, profiles: Iterable[Profile]) -> list[CliQuota]:
    """One empty CliQuota shell per CLI assigned to a role of the general config or of a profile."""
    first_enabled = bool(cfg.first_prompt.strip())
    found: dict[str, CliQuota] = {}

    def shell(cli: str) -> CliQuota:
        if cli not in found:
            found[cli] = CliQuota(cli=cli)
        return found[cli]

    for name, role in role_items(cfg):
        cli = (role.cli or "").strip()
        if not cli or (name == "first" and not first_enabled):
            continue
        shell(cli).used_by_default = True
    for profile in profiles:
        for name, role in role_items(profile):
            cli = (role.cli or "").strip()
            if not cli or (name == "first" and not first_enabled):
                continue
            entry = shell(cli)
            if profile.name not in entry.profiles:
                entry.profiles.append(profile.name)

    order = {cli: idx for idx, cli in enumerate(KNOWN_CLIS)}
    result = sorted(found.values(), key=lambda q: (order.get(q.cli, len(order)), q.cli))
    for entry in result:
        try:
            entry.installed = bool(get_driver(entry.cli).is_available())
        except KeyError:
            entry.installed = False
    return result


# ---------------------------------------------------------------- locations
def claude_credentials_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / ".credentials.json"


def kimi_home() -> Path:
    return Path(os.environ.get("KIMI_CODE_HOME") or str(Path.home() / ".kimi-code"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))


# ------------------------------------------------------------------ parsers
def parse_claude_usage(payload: dict) -> list[QuotaWindow]:
    """Windows of ``/api/oauth/usage`` (``utilization`` is already a percentage)."""
    windows: list[QuotaWindow] = []
    if not isinstance(payload, dict):
        return windows
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        item = payload.get(key)
        if isinstance(item, dict) and _is_number(item.get("utilization")):
            windows.append(QuotaWindow(
                key, _clamp_percent(item["utilization"]), _normalize_iso(item.get("resets_at")),
            ))
    extra = payload.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True and _is_number(
        extra.get("utilization")
    ):
        windows.append(QuotaWindow("extra_usage", _clamp_percent(extra["utilization"]), ""))
    return windows


_KIMI_KEYS = {
    "limit_5h": "five_hour",
    "limit_7d": "seven_day",
    "limit_month_total": "month_total",
    "limit_month_code": "month_code",
}


def parse_kimi_usage(payload: dict) -> list[QuotaWindow]:
    """Windows of the Kimi ``/usages`` endpoint (``used_ratio`` is 0-1)."""
    windows: list[QuotaWindow] = []
    usages = payload.get("usages") if isinstance(payload, dict) else None
    if not isinstance(usages, dict):
        return windows
    for source_key, key in _KIMI_KEYS.items():
        item = usages.get(source_key)
        if isinstance(item, dict) and _is_number(item.get("used_ratio")):
            windows.append(QuotaWindow(
                key,
                _clamp_percent(float(item["used_ratio"]) * 100),
                _normalize_iso(item.get("reset_time")),
            ))
    return windows


def parse_codex_rate_limits(rate_limits: dict, timestamp: str = "") -> list[QuotaWindow]:
    """Windows of a Codex ``token_count`` event (``used_percent`` is 0-100)."""
    windows: list[QuotaWindow] = []
    base = _parse_iso(timestamp)
    for slot in ("primary", "secondary"):
        item = rate_limits.get(slot) if isinstance(rate_limits, dict) else None
        if not isinstance(item, dict) or not _is_number(item.get("used_percent")):
            continue
        minutes = item.get("window_minutes")
        key = {300: "five_hour", 10080: "seven_day"}.get(
            int(minutes) if _is_number(minutes) else -1, slot,
        )
        resets = ""
        if _is_number(item.get("resets_at")):
            resets = _iso_from_epoch(float(item["resets_at"]))
        elif _is_number(item.get("resets_in_seconds")) and base is not None:
            resets = (base + timedelta(seconds=float(item["resets_in_seconds"]))).isoformat(
                timespec="seconds"
            )
        windows.append(QuotaWindow(key, _clamp_percent(item["used_percent"]), resets))
    return windows


# ------------------------------------------------------- passive observation
_OBSERVED: dict[str, CliQuota] = {}


def observe_claude_rate_limit(info: dict) -> None:
    """Remember the windows of a Claude ``rate_limit_event`` (ratio 0-1, epoch seconds)."""
    unified = info.get("unifiedWindows") if isinstance(info, dict) else None
    if not isinstance(unified, dict):
        return
    windows: list[QuotaWindow] = []
    for key, item in unified.items():
        if key not in WINDOW_KEYS or not isinstance(item, dict):
            continue
        if not _is_number(item.get("utilization")):
            continue
        resets = _iso_from_epoch(float(item["resetsAt"])) if _is_number(
            item.get("resetsAt")
        ) else ""
        windows.append(QuotaWindow(key, _clamp_percent(float(item["utilization"]) * 100), resets))
    if windows:
        _OBSERVED["claude"] = CliQuota(
            "claude", STATUS_OK, "stream", windows, fetched_at=_now_iso(),
        )


def observed(cli: str) -> CliQuota | None:
    return _OBSERVED.get(cli)


def clear_observed() -> None:
    """Forget the passive observations (tests)."""
    _OBSERVED.clear()


# ---------------------------------------------------------------- providers
def _http_json(url: str, headers: dict[str, str]) -> tuple[dict | None, CliQuota | None]:
    """GET ``url``; returns ``(payload, None)`` or ``(None, failure result)``."""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        body = default_opener(request, FETCH_TIMEOUT)
        data = json.loads(body)
    except urllib.error.HTTPError as exc:  # before URLError/OSError (subclass)
        if exc.code in (401, 403):
            return None, CliQuota("", STATUS_AUTH_EXPIRED, message=f"HTTP {exc.code}")
        return None, CliQuota("", STATUS_ERROR, message=f"HTTP {exc.code}")
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return None, CliQuota("", STATUS_ERROR, message=type(exc).__name__)
    if not isinstance(data, dict):
        return None, CliQuota("", STATUS_ERROR, message="unexpected payload")
    return data, None


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _claude_active() -> CliQuota:
    creds = _read_json(claude_credentials_path())
    oauth = creds.get("claudeAiOauth") if creds else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not token or not isinstance(token, str):
        return CliQuota("claude", STATUS_NO_CREDENTIALS)
    expires = oauth.get("expiresAt")
    if _is_number(expires) and float(expires) / 1000.0 <= time.time():  # milliseconds
        return CliQuota("claude", STATUS_AUTH_EXPIRED, message="token expired")
    payload, failure = _http_json(CLAUDE_USAGE_URL, {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    if failure is not None:
        failure.cli = "claude"
        return failure
    windows = parse_claude_usage(payload or {})
    if not windows:
        return CliQuota("claude", STATUS_NO_DATA, "api", message="no usage windows reported")
    return CliQuota("claude", STATUS_OK, "api", windows, fetched_at=_now_iso())


def _fetch_claude(opener: Opener) -> CliQuota:
    result = _claude_active()
    if result.status != STATUS_OK:
        seen = observed("claude")
        if seen is not None:
            return CliQuota(
                "claude", STATUS_OK, "stream", list(seen.windows),
                message=f"api: {result.status}", fetched_at=seen.fetched_at,
            )
    return result


def _fetch_kimi(opener: Opener) -> CliQuota:
    token = ""
    expires: Any = None
    directory = kimi_home() / "credentials"
    try:
        files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for path in files:
        data = _read_json(path)
        if data and isinstance(data.get("access_token"), str) and data["access_token"]:
            token = data["access_token"]
            expires = data.get("expires_at")
            break
    if not token:
        return CliQuota("kimi", STATUS_NO_CREDENTIALS)
    if _is_number(expires) and float(expires) <= time.time() + 30:  # seconds
        return CliQuota(
            "kimi", STATUS_AUTH_EXPIRED,
            message="token expired; it is refreshed the next time kimi runs",
        )
    base = os.environ.get("KIMI_CODE_BASE_URL", KIMI_DEFAULT_BASE_URL).rstrip("/")
    payload, failure = _http_json(base + "/usages", {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    if failure is not None:
        failure.cli = "kimi"
        return failure
    windows = parse_kimi_usage(payload or {})
    if not windows:
        return CliQuota("kimi", STATUS_NO_DATA, "api", message="no usage windows reported")
    return CliQuota("kimi", STATUS_OK, "api", windows, fetched_at=_now_iso())


def _fetch_codex(opener: Opener) -> CliQuota:
    try:
        files = list(codex_home().glob("sessions/*/*/*/rollout-*.jsonl"))
        newest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    except OSError:
        newest = None
    if newest is None:
        return CliQuota("codex", STATUS_NO_DATA, message="no rollout files")
    try:
        with newest.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - CODEX_TAIL_BYTES)
            handle.seek(start)
            raw = handle.read()
    except OSError as exc:
        return CliQuota("codex", STATUS_ERROR, message=type(exc).__name__)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]  # first line may be cut in the middle
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        payload = entry.get("payload") if isinstance(entry, dict) else None
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        limits = payload.get("rate_limits")
        if not isinstance(limits, dict):
            continue
        timestamp = str(entry.get("timestamp", ""))
        windows = parse_codex_rate_limits(limits, timestamp)
        if windows:
            return CliQuota(
                "codex", STATUS_OK, "rollout", windows,
                fetched_at=_normalize_iso(timestamp) or _now_iso(),
            )
    return CliQuota("codex", STATUS_NO_DATA, message="no rate limit event found")


_PROVIDERS: dict[str, Callable[[Opener], CliQuota]] = {
    "claude": _fetch_claude,
    "kimi": _fetch_kimi,
    "codex": _fetch_codex,
}


async def fetch_cli(shell: CliQuota) -> CliQuota:
    """Fetch one CLI (unsupported/not installed are resolved without I/O); never raises."""
    if not shell.installed:
        result = CliQuota(shell.cli, STATUS_NOT_INSTALLED)
    elif shell.cli not in _PROVIDERS:
        result = CliQuota(shell.cli, STATUS_UNSUPPORTED)
    else:
        provider = _PROVIDERS[shell.cli]
        try:
            result = await asyncio.to_thread(provider, default_opener)
        except Exception as exc:  # noqa: BLE001 - a provider never breaks the report
            result = CliQuota(shell.cli, STATUS_ERROR, message=type(exc).__name__)
    result.cli = shell.cli
    result.installed = shell.installed
    result.used_by_default = shell.used_by_default
    result.profiles = list(shell.profiles)
    return result


# ------------------------------------------------------------------ monitor
class QuotaMonitor:
    """Per-instance holder of the last quota snapshot (one per GrafenoApp)."""

    def __init__(
        self,
        collect: Callable[[], list[CliQuota]] | None = None,
        fetch: Callable[[CliQuota], Awaitable[CliQuota]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._collect = collect or self._default_collect
        self._fetch = fetch or fetch_cli
        self._clock = clock
        self._lock: asyncio.Lock | None = None
        self._results: list[CliQuota] | None = None
        self._fetched_at = ""
        self._refreshed = 0.0       # clock value of the last refresh
        self._last_live: float | None = None
        self._last_good: dict[str, CliQuota] = {}

    @staticmethod
    def _default_collect() -> list[CliQuota]:
        return configured_clis(config_mod.load(), profiles_mod.load_global())

    def _payload(
        self, mode: str, throttled: bool = False, retry_after: int = 0, warning: str = "",
    ) -> dict:
        now = datetime.now().astimezone()
        return {
            "fetched_at": self._fetched_at,
            "refresh_seconds": REFRESH_SECONDS,
            "live_min_interval": LIVE_MIN_INTERVAL,
            "mode": mode,
            "throttled": throttled,
            "retry_after": retry_after,
            "warning": warning,
            "clis": [item.to_dict(now) for item in (self._results or [])],
        }

    def peek(self) -> dict | None:
        """Last payload without I/O (None before the first refresh)."""
        if self._results is None:
            return None
        return self._payload("cached")

    def _merge_stale(self, results: list[CliQuota]) -> list[CliQuota]:
        merged: list[CliQuota] = []
        for item in results:
            if item.status == STATUS_OK and item.windows:
                self._last_good[item.cli] = item
            elif item.status in (STATUS_AUTH_EXPIRED, STATUS_ERROR) and item.cli in self._last_good:
                good = self._last_good[item.cli]
                item.windows = list(good.windows)
                item.fetched_at = good.fetched_at
                item.source = item.source or good.source
                item.stale = True
            merged.append(item)
        return merged

    async def refresh(self) -> dict:
        """Fetch every configured CLI now (single-flight)."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            recent = self._results is not None and (
                self._clock() - self._refreshed < JUST_FETCHED_SECONDS
            )
            if not recent:
                shells = self._collect()
                results = await asyncio.gather(*(self._fetch(s) for s in shells))
                self._results = self._merge_stale(list(results))
                self._fetched_at = _now_iso()
                self._refreshed = self._clock()
        return self._payload("cached")

    async def get(self, live: bool = True) -> dict:
        """API entry point: live (throttled) or cached snapshot."""
        if not live:
            if self._results is None or self._clock() - self._refreshed >= REFRESH_SECONDS:
                await self.refresh()
            return self._payload("cached")
        if self._results is not None and self._last_live is not None:
            elapsed = self._clock() - self._last_live
            if elapsed < LIVE_MIN_INTERVAL:
                retry_after = max(1, math.ceil(LIVE_MIN_INTERVAL - elapsed))
                warning = (
                    f"Live usage was fetched {int(elapsed)}s ago; serving the cached "
                    f"snapshot. Retry in {retry_after}s."
                )
                return self._payload("live", True, retry_after, warning)
        self._last_live = self._clock()
        await self.refresh()
        return self._payload("live")


# ------------------------------------------------------------- presentation
def _translate_or_raw(prefix: str, key: str) -> str:
    label = t(f"{prefix}.{key}")
    return key if label == f"{prefix}.{key}" else label


def window_label(key: str) -> str:
    """Translated window name; unknown keys are shown raw."""
    return _translate_or_raw("reports.quota.window", key)


def status_label(status: str) -> str:
    """Translated status; unknown statuses are shown raw."""
    return _translate_or_raw("reports.quota.status", status)


def used_by_label(cli: dict) -> str:
    """'Default, Profile A' from a serialized CliQuota."""
    parts: list[str] = []
    if cli.get("used_by_default"):
        parts.append(t("reports.quota.default"))
    parts.extend(str(name) for name in cli.get("profiles", []) or [])
    return ", ".join(parts)
