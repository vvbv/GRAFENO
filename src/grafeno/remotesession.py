"""Remote session mode: run the whole TUI against a remote ~/.grafeno.

Launched as ``grafeno [user@]host[:port]`` (see ``app.main``). The remote
user's ``~/.grafeno`` is mounted with sshfs and exported as ``GRAFENO_HOME``
so every data layer (config, tasks, references, triggers, logs) works on the
remote host transparently; project workdirs are remote paths mounted on
demand through the session fallback in ``remote.effective_workdir``.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import paths, remote
from .i18n import t
from .remote import RemoteSpec

if TYPE_CHECKING:
    from .models import Task

_HOST_ONLY = re.compile(
    r"^(?:(?P<user>[\w.\-]+)@)?(?P<host>[\w.\-]+)(?::(?P<port>\d+))?$"
)
PROBE_TIMEOUT_S = 20

_current: "RemoteSession | None" = None


class SessionError(Exception):
    """The remote session could not be established (fatal at startup)."""


@dataclass
class RemoteSession:
    """Active remote session: connection data and local mount points."""

    spec: RemoteSpec          # host-level spec; path = remote $HOME
    identity: str = ""
    password: str = ""        # in-memory only
    remote_home: str = ""     # $HOME on the remote host
    remote_os: str = ""       # probed once at bootstrap
    home_mount: Path | None = None  # local sshfs mount of remote ~/.grafeno


def parse_host_spec(text: str) -> RemoteSpec | None:
    """Parse a session host spec; ``None`` for non-host inputs.

    Accepts ``[user@]host``, ``[user@]host:port`` and the bare
    ``ssh://[user@]host[:port][/]`` form. Anything that looks like a
    scp-like remote location (host:/path) is rejected here: the CLI
    starts a session, not a single remote task.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    match = _HOST_ONLY.match(cleaned)
    if match:
        groups = match.groupdict()
        return RemoteSpec(
            user=groups.get("user") or "",
            host=groups.get("host") or "",
            port=int(groups.get("port") or 0),
            path="",
        )
    # ssh://[user@]host[:port][/]  -- only the host part; path must be empty.
    spec = remote.parse_spec(cleaned)
    if spec is None:
        return None
    if spec.path not in ("", "/"):
        return None
    spec.path = ""
    return spec


def sessions_base() -> Path:
    """Local-real base for session mounts (avoids mounting sshfs on sshfs).

    This MUST be evaluated before ``activate`` rewrites ``GRAFENO_HOME``;
    ``bootstrap`` only calls it once on entry, so the order is safe.
    """
    override = os.environ.get(paths.ENV_HOME, "")
    base = Path(override) if override else Path.home() / ".grafeno"
    return base / "sessions"


def current() -> "RemoteSession | None":
    return _current


def active() -> bool:
    return _current is not None


def label() -> str:
    return _current.spec.target if _current is not None else ""


def spec_for_task(task: "Task") -> RemoteSpec | None:
    """Resolve the spec to use for ``task`` under the active session.

    A task with an explicit ``task.remote`` keeps it (it wins); otherwise
    in session mode the task's ``workdir`` is mounted under the session
    target with relative paths anchored at the remote ``$HOME``.
    """
    explicit = remote.parse_spec(task.remote)
    if explicit is not None:
        return explicit
    session = _current
    if session is None:
        return None
    workdir = task.workdir or "."
    if workdir.startswith("/"):
        path = workdir.rstrip("/") or "/"
    else:
        path = f"{session.remote_home.rstrip('/')}/{workdir}"
    return RemoteSpec(
        user=session.spec.user,
        host=session.spec.host,
        port=session.spec.port,
        path=path,
    )


def describe_target(task: "Task") -> str:
    """Stable target string for prompts and the UI."""
    if task.remote:
        return task.remote
    if _current is not None:
        return _current.spec.target
    return ""


async def bootstrap(
    spec: RemoteSpec,
    *,
    identity: str = "",
    password: str = "",
    on_info: Callable[[str], None] = lambda m: None,
) -> RemoteSession:
    """Establish the remote session (mounts the remote ``~/.grafeno``)."""
    # 1. Required tools. ssh/sshfs are mandatory; sshpass only when the
    #    session authenticates with a password (not identity files).
    if shutil.which("ssh") is None:
        raise SessionError(t("rsession.no_tool", tool="ssh"))
    if not remote.sshfs_available():
        raise SessionError(t("rsession.no_tool", tool="sshfs"))
    if password and not remote.sshpass_available():
        raise SessionError(t("rsession.no_tool", tool="sshpass"))

    # 2. Register the session BEFORE any mounting/probe so the
    #    ssh/sshfs commands inherit the auth options and ``mount_dir``
    #    uses the session's mounts base (outside the remote GRAFENO_HOME).
    base = sessions_base()
    remote.set_session(spec, identity=identity, password=password, mounts_base=base)

    # 3. Self-host shortcut: nothing to mount; ``home_mount`` points at the
    #    actual local GRAFENO_HOME (where activate() will redirect to).
    if remote.is_self(spec):
        session = RemoteSession(
            spec=RemoteSpec(
                user=spec.user, host=spec.host, port=spec.port, path=str(Path.home())
            ),
            identity=identity,
            password=password,
            remote_home=str(Path.home()),
            remote_os="",
            home_mount=Path.home() / ".grafeno",
        )
        return session

    # 4. Probe the remote $HOME over ssh.
    code, out = await remote.run_remote_command(spec, "echo $HOME", PROBE_TIMEOUT_S)
    if code != 0 or not out.strip():
        raise SessionError(t("rsession.connect.fail", target=spec.target))
    remote_home = out.strip().splitlines()[0].strip()
    spec.path = remote_home  # absolute path the session is anchored to

    # 5. Ensure the remote GRAFENO_HOME exists (best effort: the mount
    #    below will surface real failures).
    await remote.run_remote_command(
        spec, 'mkdir -p "$HOME/.grafeno"', PROBE_TIMEOUT_S
    )

    # 6. Mount the remote ~/.grafeno with sshfs. ``mount_dir`` produces a
    #    deterministic local point under ``mounts_base()`` (the session
    #    base registered above, not the soon-to-be-rewritten GRAFENO_HOME).
    grafeno_spec = RemoteSpec(
        user=spec.user, host=spec.host, port=spec.port, path=f"{remote_home}/.grafeno"
    )
    home_mount = remote.mount_dir(grafeno_spec)
    ok = await remote.ensure_mounted(grafeno_spec, on_info=on_info)
    if not ok:
        raise SessionError(t("rsession.mount.fail", target=spec.target))

    # 7. Probe the remote OS once (best effort).
    remote_os = await remote.detect_os(spec)

    session = RemoteSession(
        spec=spec,
        identity=identity,
        password=password,
        remote_home=remote_home,
        remote_os=remote_os,
        home_mount=home_mount,
    )
    return session


def activate(session: RemoteSession) -> None:
    """Activate the session: redirect GRAFENO_HOME to the remote mount."""
    global _current
    _current = session
    if session.home_mount is not None:
        os.environ[paths.ENV_HOME] = str(session.home_mount)


def deactivate() -> None:
    """Clear the session and forget GRAFENO_HOME (tests use this)."""
    global _current
    os.environ.pop(paths.ENV_HOME, None)
    _current = None
    remote.clear_session()
