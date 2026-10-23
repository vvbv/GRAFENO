"""Project creation inside the configured working directories (workspaces).

A project is a first-level folder of a working directory. It can be
created empty (``plain``), initialized as a git repository (``init``, the
default) or cloned from an HTTP(S)/SSH URL (``clone``). Names, URLs and
working directories are validated before touching the disk; git runs as
an asyncio subprocess without a shell, without prompts, without a
controlling terminal (a timeout or cancellation kills the whole process
tree) and limited to the http/https/ssh transports. Errors are raised as
``ProjectError`` with a stable ``code`` that the API maps to an HTTP status.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .workspaces import Root, entry_key

MODES = ("init", "clone", "plain")  # init = folder + git init, clone = git clone, plain = folder only
DEFAULT_MODE = "init"
URL_SCHEMES = ("https", "http", "ssh")  # plus scp-like "user@host:path"
NAME_MAX_LENGTH = 100
NAME_MAX_BYTES = 255  # typical file system limit (UTF-8 bytes)
URL_MAX_LENGTH = 2048
INIT_TIMEOUT = 30.0  # seconds
CLONE_TIMEOUT = 600.0  # seconds
KILL_GRACE = 5.0  # seconds to wait for git once its process tree was killed
SSH_CONNECT_TIMEOUT = 30  # seconds (default GIT_SSH_COMMAND only)
HTTP_LOW_SPEED_LIMIT = "1000"  # bytes/s; git aborts HTTP transfers slower than this ...
HTTP_LOW_SPEED_TIME = "60"  # ... for this many seconds
TIMED_OUT = -1000  # outside the -signal range (-N = killed by signal N); _run_git return code when git exceeded its timeout (-1 = could not run)
ALLOWED_PROTOCOLS = "http:https:ssh"  # GIT_ALLOW_PROTOCOL (also applies to submodules)
_FORBIDDEN_CHARS = frozenset('<>:"/\\|?*')
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)  # Windows device names
_SCP_URL = re.compile(
    r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?"  # optional user
    r"[A-Za-z0-9][A-Za-z0-9.-]+"           # host (2+ chars: never a drive letter)
    r":(?!/)[^\s]+"                        # path, not "//" (that is a scheme URL)
)


class ProjectError(Exception):
    """Validation or creation failure; ``code`` is stable, ``message`` is user facing."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreatedProject:
    """Result of ``create_project``."""

    name: str
    path: Path       # created folder (``workspace / name``)
    workspace: Path  # resolved working directory
    mode: str        # one of MODES
    git: bool        # True when the folder is a git repository
    url: str = ""    # redacted clone URL ("" unless mode == "clone")


def _has_control(text: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def validate_name(raw: str) -> str:
    """Clean project folder name or ``ProjectError("invalid_name")``."""
    name = raw.strip()

    def bad(reason: str) -> ProjectError:
        return ProjectError("invalid_name", f"invalid project name: {reason}")

    if not name:
        raise ProjectError("invalid_name", "name is required")
    if len(name) > NAME_MAX_LENGTH:
        raise bad(f"longer than {NAME_MAX_LENGTH} characters")
    if name in (".", ".."):
        raise bad("reserved name")
    if name.startswith("."):
        raise bad("must not start with a dot")
    if name.startswith("-"):
        raise bad("must not start with a dash")
    if len(name.encode("utf-8")) > NAME_MAX_BYTES:
        raise bad("too long for the file system")
    if any(c in _FORBIDDEN_CHARS for c in name):
        raise bad("forbidden character")
    if _has_control(name):
        raise bad("control character")
    if name.endswith((".", " ")):
        raise bad("must not end with a dot or space")
    if name.split(".")[0].casefold() in _RESERVED_NAMES:
        raise bad("reserved device name")
    return name


def validate_url(raw: str) -> str:
    """Clean clone URL or ``ProjectError("invalid_url")``."""
    url = raw.strip()
    if not url:
        raise ProjectError("invalid_url", "url is required")
    if len(url) > URL_MAX_LENGTH:
        raise ProjectError("invalid_url", "url is too long")
    if url.startswith("-"):
        raise ProjectError("invalid_url", "invalid url")
    if any(c.isspace() for c in url) or _has_control(url):
        raise ProjectError("invalid_url", "url must not contain spaces or control characters")
    if "::" in url:
        raise ProjectError("invalid_url", "unsupported url transport")
    if "://" in url:
        try:
            parts = urlsplit(url)
            hostname = parts.hostname
            parts.port  # noqa: B018 - raises ValueError on an invalid port
        except ValueError as exc:
            raise ProjectError("invalid_url", "invalid url") from exc
        if parts.scheme.lower() not in URL_SCHEMES:
            raise ProjectError(
                "invalid_url", "unsupported url: use https://, http://, ssh:// or user@host:path"
            )
        if not hostname or hostname.startswith("-"):
            raise ProjectError("invalid_url", "invalid url host")
        if not parts.path.strip("/"):
            raise ProjectError("invalid_url", "url has no repository path")
        return url
    if not _SCP_URL.fullmatch(url):
        raise ProjectError(
            "invalid_url", "unsupported url: use https://, http://, ssh:// or user@host:path"
        )
    return url


def redact_url(url: str) -> str:
    """Mask credentials of a URL (``https://user:tok@host`` -> ``user:***``). Never raises."""
    try:
        if "://" not in url:
            return url
        parts = urlsplit(url)
        if "@" not in parts.netloc:
            return url
        userinfo, _, host = parts.netloc.rpartition("@")
        if ":" in userinfo:
            masked = userinfo.split(":", 1)[0] + ":***"
        elif parts.scheme.lower() in ("http", "https"):
            masked = "***"  # a lone user is usually a token
        else:
            return url
        return url.replace(parts.netloc, f"{masked}@{host}", 1)
    except Exception:  # noqa: BLE001 - redaction must never fail
        return url


def name_from_url(url: str) -> str:
    """Repository name from a clone URL ("" when there is none)."""
    if "://" in url:
        try:
            path = urlsplit(url).path
        except ValueError:
            return ""
    else:
        path = url.split(":", 1)[1] if ":" in url else url
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    if segment.endswith(".git"):
        segment = segment[: -len(".git")]
    return segment


def resolve_mode(mode: str, url: str) -> str:
    """Effective creation mode for the requested ``mode`` and ``url``."""
    value = mode.strip().lower()
    if not value:
        value = "clone" if url else DEFAULT_MODE
    if value not in MODES:
        raise ProjectError("invalid_mode", "mode must be one of: init, clone, plain")
    if value == "clone" and not url:
        raise ProjectError("invalid_url", "url is required for mode clone")
    if value != "clone" and url:
        raise ProjectError("invalid_mode", "url is only allowed with mode clone")
    return value


def resolve_workspace(requested: str, roots: list[Root]) -> Path:
    """Configured, existing working directory for ``requested`` (never an arbitrary path)."""
    candidates = [Path(r.path) for r in roots if r.exists]
    if not candidates:
        raise ProjectError("no_workspace", "no working directory configured")
    requested = requested.strip()
    if not requested:
        if len(candidates) == 1:
            return candidates[0]
        raise ProjectError(
            "invalid_workspace", "workspace is required: several working directories are configured"
        )
    try:
        resolved: Path | None = Path(requested).expanduser().resolve()
    except (OSError, RuntimeError):
        resolved = None
    for root in roots:
        if not root.exists:
            continue
        if entry_key(requested) == entry_key(root.entry) or resolved == Path(root.path):
            return Path(root.path)
    raise ProjectError("invalid_workspace", "workspace is not a configured working directory")


def git_available() -> bool:
    """True when git is installed on the host."""
    return shutil.which("git") is not None


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_ALLOW_PROTOCOL"] = ALLOWED_PROTOCOLS
    env["GIT_ASKPASS"] = ""  # empty value also overrides core.askPass and SSH_ASKPASS: no GUI prompts
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = f"ssh -o BatchMode=yes -o ConnectTimeout={SSH_CONNECT_TIMEOUT}"  # no prompts
    env.setdefault("GIT_HTTP_LOW_SPEED_LIMIT", HTTP_LOW_SPEED_LIMIT)
    env.setdefault("GIT_HTTP_LOW_SPEED_TIME", HTTP_LOW_SPEED_TIME)
    return env


def _tail(text: str, limit: int = 600) -> str:
    """Last non-empty lines of ``text`` up to roughly ``limit`` characters."""
    lines = [line for line in text.splitlines() if line.strip()]
    picked: list[str] = []
    size = 0
    for line in reversed(lines):
        if picked and size + len(line) > limit:
            break
        picked.append(line)
        size += len(line) + 1
    return "\n".join(reversed(picked))[-limit:]


def _spawn_options() -> dict[str, object]:
    """Detach git from the TUI terminal and give it its own process group."""
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}  # no controlling tty: ssh/helpers cannot block on /dev/tty


async def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill git and every helper it spawned (ssh, git-remote-https), then wait briefly."""
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill:  # /T needs the parent alive: run it before process.kill()
            with contextlib.suppress(OSError, asyncio.TimeoutError):
                killer = await asyncio.create_subprocess_exec(
                    taskkill, "/F", "/T", "/PID", str(process.pid),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    **_spawn_options(),
                )
                await asyncio.wait_for(killer.wait(), KILL_GRACE)
    else:
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)  # new session: pgid == pid
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(process.wait(), KILL_GRACE)  # never unbounded: orphans may hold the pipes


async def _run_process(
    argv: list[str], cwd: Path, timeout: float, env: dict[str, str]
) -> tuple[int, str]:
    """Run ``argv``; returns ``(returncode, trimmed output)``, -1 on OS error, ``TIMED_OUT`` on timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            **_spawn_options(),
        )
    except OSError as exc:
        return -1, str(exc)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        await _kill_tree(process)
        return TIMED_OUT, f"timed out after {int(timeout)} s"
    except asyncio.CancelledError:
        await _kill_tree(process)  # so the caller's cleanup does not race a live git
        raise
    text = output.decode("utf-8", errors="replace").strip()
    return process.returncode or 0, _tail(text)


async def _run_git(args: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    """Run git; returns ``(returncode, trimmed output)``, -1 if it could not run, ``TIMED_OUT`` on timeout."""
    executable = shutil.which("git")
    if executable is None:
        return -1, "git is not installed"
    return await _run_process([executable, *args], cwd, timeout, _git_env())


_RETRYABLE = (os.unlink, os.remove, os.rmdir)  # handler callbacks that are safe to call again


def _make_writable(target: str, root: str) -> None:
    """Clear read-only bits on ``target`` and, when it lies inside ``root``, on its parent."""
    parent = os.path.dirname(target)
    if os.path.normpath(target) != os.path.normpath(root):  # never touch the folder holding the project
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)
    with contextlib.suppress(OSError):
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


def _remove_tree(path: Path, attempts: int = 3) -> None:
    """Best-effort recursive delete that also clears read-only bits (git packs on Windows)."""
    root = str(path)

    def _retry(func, target, _exc) -> None:
        _make_writable(target, root)
        if func in _RETRYABLE:  # other callbacks (open, scandir...) are repeated by the next attempt
            with contextlib.suppress(OSError):
                func(target)

    with contextlib.suppress(OSError):
        for attempt in range(attempts):
            if not os.path.lexists(path):
                return
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_retry)
            else:
                shutil.rmtree(path, onerror=_retry)
            if not os.path.lexists(path):
                return
            if os.name == "nt" and attempt < attempts - 1:
                time.sleep(0.2)  # Windows releases git handles with a delay


async def create_project(
    name: str,
    roots: list[Root],
    *,
    workspace: str = "",
    url: str = "",
    mode: str = "",
) -> CreatedProject:
    """Create a project folder in a configured working directory.

    Everything is validated before touching the disk. When git fails, the
    folder created by this call (and only that one) is removed.
    """
    url = validate_url(url) if url.strip() else ""
    mode = resolve_mode(mode, url)
    if not name.strip() and mode == "clone":
        name = name_from_url(url)
    name = validate_name(name)
    root = resolve_workspace(workspace, roots)
    if mode != "plain" and not git_available():
        raise ProjectError("git_missing", "git is not installed")
    target = root / name
    if target.parent != root:
        raise ProjectError("invalid_name", "invalid project name: escapes the working directory")
    if os.path.lexists(target):  # also covers broken symlinks
        raise ProjectError("exists", f"project already exists: {name}")
    try:
        target.mkdir()
    except FileExistsError as exc:
        raise ProjectError("exists", f"project already exists: {name}") from exc
    except OSError as exc:
        raise ProjectError("io_error", f"cannot create folder: {exc.strerror or exc}") from exc
    ok = False
    try:
        if not target.resolve().is_relative_to(root):
            raise ProjectError("invalid_name", "invalid project name: escapes the working directory")
        if mode == "plain":
            ok = True
            return CreatedProject(name, target, root, "plain", git=False)
        if mode == "init":
            code, output = await _run_git(["init", "--quiet", str(target)], root, INIT_TIMEOUT)
            if code == TIMED_OUT:
                raise ProjectError("timeout", f"git init timed out after {int(INIT_TIMEOUT)} s")
            if code != 0:
                raise ProjectError("init_failed", "git init failed: " + output)
        else:
            code, output = await _run_git(
                ["clone", "--quiet", "--", url, str(target)], root, CLONE_TIMEOUT
            )
            if code == TIMED_OUT:
                raise ProjectError("timeout", f"git clone timed out after {int(CLONE_TIMEOUT)} s")
            if code != 0:
                raise ProjectError(
                    "clone_failed", "git clone failed: " + output.replace(url, redact_url(url))
                )
        ok = True
        return CreatedProject(
            name, target, root, mode, git=True, url=redact_url(url) if mode == "clone" else ""
        )
    finally:
        if not ok:  # failure or cancellation: drop the folder created in this call
            _remove_tree(target)
