"""Self-update of GRAFENO itself from the GitHub releases.

Best effort: every function degrades to ``None``/``ok=False`` on error and
NEVER raises towards the caller. HTTP uses the stdlib opener shared with the
Telegram client (product User-Agent, tolerant TLS policy).
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import shutil
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .i18n import t
from .telegram.api import USER_AGENT, default_opener

RELEASES_API = "https://api.github.com/repos/vvbv/GRAFENO/releases/latest"
GIT_URL = "https://github.com/vvbv/GRAFENO.git"
CHECK_TIMEOUT = 10.0    # seconds for the release check
UPDATE_TIMEOUT = 600.0  # seconds for the pip/pipx update command


@dataclass
class SelfUpdateOutcome:
    """Result of one self-update attempt."""

    version: str  # normalized target version (no leading "v")
    ok: bool
    detail: str = ""  # short error/summary text (never secrets)


def normalize_version(tag: str) -> str:
    """``"v1.42.0"`` -> ``"1.42.0"``; trims whitespace and a leading v/V."""
    value = tag.strip()
    if value[:1] in ("v", "V"):
        value = value[1:]
    return value.strip()


def parse_version(value: str) -> tuple[int, ...]:
    """Numeric tuple for comparisons; suffixes (``-beta``, ``+build``) drop."""
    core = normalize_version(value).split("+")[0].split("-")[0]
    parts: list[int] = []
    for chunk in core.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """True when ``latest`` is a strictly newer version than ``current``."""
    new, old = parse_version(latest), parse_version(current)
    length = max(len(new), len(old))
    new += (0,) * (length - len(new))
    old += (0,) * (length - len(old))
    return new > old


def _fetch_tag_sync(opener, timeout: float) -> str | None:
    """Blocking fetch of the latest release tag; ``None`` on any failure."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        payload = opener(request, timeout)
    except Exception:
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    tag = str(data.get("tag_name", "")).strip()
    return tag or None


async def fetch_latest_version(
    timeout: float = CHECK_TIMEOUT, opener=None
) -> str | None:
    """Latest release version (normalized, no ``v``) or ``None`` on error.

    ``opener`` is injectable so tests never touch the network; by default it
    reuses ``telegram.api.default_opener`` (same TLS/User-Agent policy).
    """
    tag = await asyncio.to_thread(
        _fetch_tag_sync, opener or default_opener, timeout
    )
    return normalize_version(tag) if tag else None


def installed_via_pipx() -> bool:
    """True when the running GRAFENO interpreter lives in a pipx-managed venv."""
    parts = [part.casefold() for part in Path(sys.prefix).parts]
    return "pipx" in parts


def installed_version() -> str | None:
    """Installed ``grafeno`` distribution version; ``None`` when unqueryable."""
    try:
        return importlib.metadata.version("grafeno")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_update_command(version: str) -> list[str]:
    """Update command matching how GRAFENO is actually installed.

    pipx only when the running interpreter is a pipx-managed venv (choosing
    pipx otherwise installed a second, disconnected copy and reported a
    false success), else the current interpreter's pip. pip gets
    ``--force-reinstall`` because ``--upgrade`` alone is not deterministic
    with git-URL requirements: it can leave the same version in place and
    still exit 0.
    """
    tag = f"v{normalize_version(version)}"
    url = f"git+{GIT_URL}@{tag}"
    pipx = shutil.which("pipx") if installed_via_pipx() else None
    if pipx:
        return [pipx, "install", "--force", url]
    return [
        sys.executable, "-m", "pip", "install",
        "--upgrade", "--force-reinstall", url,
    ]


async def _run_command(command: list[str], timeout: float) -> tuple[int, str]:
    """Run the update command; returns (returncode, last output line)."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timeout"
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    tail = stdout.decode("utf-8", errors="replace").strip().splitlines()
    return process.returncode or 0, (tail[-1][:200] if tail else "")


async def run_self_update(
    version: str, timeout: float = UPDATE_TIMEOUT
) -> SelfUpdateOutcome:
    """Update GRAFENO to ``version`` and verify the installed version afterwards.

    A bare exit code is not trustworthy (pip/pipx can exit 0 while leaving
    the old version in place): after a successful command the installed
    ``grafeno`` metadata is compared with the target and a mismatch is
    reported as a failed update.
    """
    target = normalize_version(version)
    command = build_update_command(version)
    try:
        code, detail = await _run_command(command, timeout)
    except OSError as exc:
        return SelfUpdateOutcome(version=target, ok=False, detail=str(exc))
    if code != 0:
        return SelfUpdateOutcome(version=target, ok=False, detail=detail)
    installed = installed_version()
    # No distribution metadata: trust the exit code (nothing to compare).
    if installed is not None and normalize_version(installed) != target:
        return SelfUpdateOutcome(
            version=target,
            ok=False,
            detail=t("supd.verify_failed", version=installed),
        )
    return SelfUpdateOutcome(version=target, ok=True, detail=detail)


def cli_update() -> int:
    """Entry point of ``grafeno update``: manual self-update, returns exit code.

    Loads the config only to honor the configured language; it never opens
    the TUI and never touches the remote-session bootstrap.
    """
    from . import config as config_module
    from .i18n import set_language, t

    cfg = config_module.load()
    set_language(cfg.language)
    latest = asyncio.run(fetch_latest_version())
    if latest is None:
        print(t("supd.check_failed"))
        return 1
    from . import __version__

    if not is_newer(latest, __version__):
        print(t("supd.latest", version=__version__))
        return 0
    outcome = asyncio.run(run_self_update(latest))
    if outcome.ok:
        print(t("supd.done", version=outcome.version))
        return 0
    print(t("supd.failed", error=outcome.detail or "?"))
    return 1