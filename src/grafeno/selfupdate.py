"""Self-update of GRAFENO itself from the GitHub releases.

Best effort: every function degrades to ``None``/``ok=False`` on error and
NEVER raises towards the caller. HTTP uses the stdlib opener shared with the
Telegram client (product User-Agent, tolerant TLS policy).
"""

from __future__ import annotations

import asyncio
import codecs
import collections.abc
import importlib.metadata
import json
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .i18n import t
from .telegram.api import (
    USER_AGENT,
    _unverified_context,
    default_opener,
    is_cert_error,
    ssl_context,
)

RELEASES_API = "https://api.github.com/repos/vvbv/GRAFENO/releases/latest"
RELEASES_PAGE = "https://github.com/vvbv/GRAFENO/releases/latest"  # web redirect, no API rate limit
WHEEL_NAME = "grafeno-{version}-py3-none-any.whl"
GIT_URL = "https://github.com/vvbv/GRAFENO.git"
CHECK_TIMEOUT = 10.0    # seconds for the release check (per attempt)
CHECK_ATTEMPTS = 3      # check attempts before giving up (transient hiccups)
CHECK_BACKOFF = 2.0     # seconds between check attempts
UPDATE_TIMEOUT = 600.0  # seconds for the pip/pipx update command

_PROGRESS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TAG_URL_RE = re.compile(r"/releases/tag/([^/?#]+)/?$")
_BOX_CHARS = " \t│╰─>×║"  # pip's rich error decorations
_DIAG_CODE_RE = re.compile(r"^error: [a-z0-9]+(-[a-z0-9]+)+$")  # e.g. subprocess-exited-with-error


@dataclass
class SelfUpdateOutcome:
    """Result of one self-update attempt."""

    version: str  # normalized target version (no leading "v")
    ok: bool
    detail: str = ""  # short error/summary text (never secrets)


@dataclass
class ReleaseInfo:
    """Latest release lookup result; ``version`` is empty when it failed."""

    version: str = ""    # normalized version (no leading "v")
    wheel_url: str = ""  # release wheel asset URL, empty when absent
    error: str = ""      # short failure reason when ``version`` is empty


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


def extract_percent(text: str) -> float | None:
    """Last percentage marker in a raw output chunk; ``None`` when absent."""
    matches = _PROGRESS_RE.findall(text)
    return float(matches[-1]) if matches else None


def render_progress(percent: float, width: int = 30) -> str:
    """ASCII progress bar, e.g. ``[########----------------] 32%``."""
    clamped = max(0.0, min(percent, 100.0))
    filled = round(clamped / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {clamped:.0f}%"


class TerminalProgress:
    """One-line terminal progress bar; ``written`` records any draw."""

    def __init__(self) -> None:
        self.written = False

    def feed(self, text: str) -> None:
        """Redraw the bar when a percentage marker appears in ``text``."""
        percent = extract_percent(text)
        if percent is None:
            return
        self.written = True
        sys.stdout.write("\r" + render_progress(percent))
        sys.stdout.flush()


def _http_reason(exc: urllib.error.HTTPError) -> str:
    """Readable reason for a GitHub API HTTP error (rate limit included)."""
    remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
    if exc.code in (403, 429) and remaining == "0":
        return f"GitHub API rate limit exceeded (HTTP {exc.code})"
    return f"GitHub API HTTP {exc.code}"


def _wheel_asset_url(data: dict, version: str) -> str:
    """Download URL of the release wheel matching ``version``; "" when absent."""
    expected = WHEEL_NAME.format(version=version)
    assets = data.get("assets")
    if not isinstance(assets, list):
        return ""
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("name") != expected:
            continue
        url = str(asset.get("browser_download_url", "")).strip()
        if url.startswith("https://"):
            return url
    return ""


def _fetch_release_sync(opener, timeout: float) -> ReleaseInfo:
    """Blocking fetch of the latest release; ``error`` explains any failure."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        payload = opener(request, timeout)
    except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
        return ReleaseInfo(error=_http_reason(exc))
    except urllib.error.URLError as exc:
        return ReleaseInfo(error=f"network error: {exc.reason}")
    except Exception as exc:
        return ReleaseInfo(error=f"{type(exc).__name__}: {exc}")
    try:
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ReleaseInfo(error="invalid GitHub API response")
    tag = str(data.get("tag_name", "")).strip() if isinstance(data, dict) else ""
    if not tag:
        return ReleaseInfo(error="no release tag in GitHub API response")
    version = normalize_version(tag)
    return ReleaseInfo(version=version, wheel_url=_wheel_asset_url(data, version))


def _open_final_url(request: urllib.request.Request, timeout: float) -> str:
    """Final URL after redirects, with the shared TLS policy (cert fallback)."""
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            return response.geturl()
    except (urllib.error.URLError, OSError) as exc:
        if not is_cert_error(exc):
            raise
        with urllib.request.urlopen(
            request, timeout=timeout, context=_unverified_context()
        ) as response:
            return response.geturl()


def tag_from_release_url(url: str) -> str | None:
    """``.../releases/tag/v1.2.3`` -> ``"1.2.3"``; ``None`` when it does not match."""
    match = _TAG_URL_RE.search(url or "")
    if not match:
        return None
    version = normalize_version(urllib.parse.unquote(match.group(1)))
    return version or None


def _latest_from_redirect(timeout: float) -> str | None:
    """Latest version from the web releases page redirect; ``None`` on error."""
    request = urllib.request.Request(RELEASES_PAGE, headers={"User-Agent": USER_AGENT})
    try:
        return tag_from_release_url(_open_final_url(request, timeout))
    except Exception:
        return None


async def check_latest_release(
    timeout: float = CHECK_TIMEOUT, opener=None
) -> ReleaseInfo:
    """Latest release (version + wheel URL) or the reason the check failed.

    Tries the GitHub API first; when it fails (rate limit, HTTP error, network)
    it falls back to the web releases page redirect, which is not rate
    limited (no wheel URL in that case: the update uses the git tag). Retried
    ``CHECK_ATTEMPTS`` times with ``CHECK_BACKOFF`` between attempts.
    """
    attempts = max(1, CHECK_ATTEMPTS)
    info = ReleaseInfo(error="unknown error")
    for index in range(attempts):
        info = await asyncio.to_thread(
            _fetch_release_sync, opener or default_opener, timeout
        )
        if info.version:
            return info
        fallback = await asyncio.to_thread(_latest_from_redirect, timeout)
        if fallback:
            return ReleaseInfo(version=fallback)
        if index < attempts - 1 and CHECK_BACKOFF > 0:
            await asyncio.sleep(CHECK_BACKOFF)
    return info


async def fetch_latest_version(
    timeout: float = CHECK_TIMEOUT, opener=None
) -> str | None:
    """Latest release version (normalized) or ``None``; see ``check_latest_release``."""
    info = await check_latest_release(timeout, opener)
    return info.version or None


def installed_via_pipx() -> bool:
    """True when the running GRAFENO interpreter lives in a pipx-managed venv."""
    parts = [
        part.casefold() for part in re.split(r"[\\/]", sys.prefix) if part
    ]
    return "pipx" in parts


def installed_version() -> str | None:
    """Installed ``grafeno`` distribution version; ``None`` when unqueryable."""
    try:
        return importlib.metadata.version("grafeno")
    except importlib.metadata.PackageNotFoundError:
        return None


def build_update_command(version: str, wheel_url: str = "") -> list[str]:
    """Update command matching how GRAFENO is actually installed.

    pipx only when the running interpreter is a pipx-managed venv (choosing
    pipx otherwise installed a second, disconnected copy and reported a
    false success), else the current interpreter's pip. pip gets
    ``--force-reinstall`` because ``--upgrade`` alone is not deterministic
    with git-URL requirements: it can leave the same version in place and
    still exit 0. ``--progress-bar=on`` forces percentage output even when
    stdout is piped, so the CLI can draw a live progress bar; with pipx the
    flag reaches pip through ``--pip-args`` as a single ``--pip-args=<value>``
    token, because argparse refuses a value starting with ``-`` passed as a
    separate token (``error: argument --pip-args: expected one argument``).

    The wheel published in the release is preferred because it needs neither
    ``git`` nor a build from source; releases without an asset use the git tag.
    """
    spec = wheel_url or f"git+{GIT_URL}@v{normalize_version(version)}"
    pipx = shutil.which("pipx") if installed_via_pipx() else None
    if pipx:
        return [
            pipx, "install", "--force",
            "--pip-args=--progress-bar=on", spec,
        ]
    return [
        sys.executable, "-m", "pip", "install",
        "--upgrade", "--force-reinstall", "--progress-bar=on", spec,
    ]


def summarize_output(text: str, limit: int = 300) -> str:
    """Most relevant line of an installer output (errors over trailing notes)."""
    lines = [
        line.lstrip(_BOX_CHARS).rstrip() for line in text.replace("\r", "\n").splitlines()
    ]
    # Drop pip's bare diagnostic codes: they carry no information about the cause.
    lines = [line for line in lines if line and not _DIAG_CODE_RE.match(line)]
    if not lines:
        return ""
    for predicate in (
        lambda line: line.startswith("ERROR:") or line.startswith("error:"),
        lambda line: "did not run successfully" in line,
        lambda line: line.startswith("Fatal error"),
        lambda line: not line.startswith(("note:", "hint:")),
    ):
        matches = [line for line in lines if predicate(line)]
        if matches:
            return matches[-1][:limit]
    return lines[-1][:limit]


async def _run_command(
    command: list[str],
    timeout: float,
    on_chunk: collections.abc.Callable[[str], None] | None = None,
) -> tuple[int, str]:
    """Run the update command; returns (returncode, relevant output line).

    Output is streamed chunk by chunk to ``on_chunk`` (when given) so the
    caller can render a live progress bar while pip downloads.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    captured: list[str] = []

    async def _pump() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while chunk := await process.stdout.read(4096):
            text = decoder.decode(chunk)
            captured.append(text)
            if on_chunk is not None:
                on_chunk(text)
        await process.wait()

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return 124, "timeout"
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, summarize_output("".join(captured))


async def run_self_update(
    version: str,
    timeout: float = UPDATE_TIMEOUT,
    on_progress: collections.abc.Callable[[str], None] | None = None,
    wheel_url: str = "",
) -> SelfUpdateOutcome:
    """Update GRAFENO to ``version`` and verify the installed version afterwards.

    A bare exit code is not trustworthy (pip/pipx can exit 0 while leaving
    the old version in place): after a successful command the installed
    ``grafeno`` metadata is compared with the target and a mismatch is
    reported as a failed update. ``on_progress`` receives raw output
    chunks while the installer runs.
    """
    target = normalize_version(version)
    command = build_update_command(version, wheel_url)
    try:
        code, detail = await _run_command(command, timeout, on_chunk=on_progress)
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
    release = asyncio.run(check_latest_release())
    if not release.version:
        print(t("supd.check_failed_detail", error=release.error or "?"))
        return 1
    latest = release.version
    from . import __version__

    if is_newer(__version__, latest):
        print(t("supd.ahead", current=__version__, latest=latest))
        return 0
    if not is_newer(latest, __version__):
        print(t("supd.latest", version=__version__))
        return 0
    print(t("supd.updating", current=__version__, latest=latest))
    progress = TerminalProgress()
    outcome = asyncio.run(
        run_self_update(latest, on_progress=progress.feed, wheel_url=release.wheel_url)
    )
    if progress.written:
        sys.stdout.write("\n")
    if outcome.ok:
        print(t("supd.done", version=outcome.version))
        return 0
    print(t("supd.failed", error=outcome.detail or "?"))
    return 1