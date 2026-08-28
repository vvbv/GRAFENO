"""Remote project support: SSH locations, sshfs mounts and task-data sync.

A task may point to a remote project expressed as a scp-like spec
``[user@]host:/abs/path`` or an ``ssh://[user@]host[:port]/abs/path`` URL.
The remote directory is mounted locally with sshfs under
``~/.grafeno/mounts/`` so the agent CLIs keep working on a plain local path
(the most native option for them). Task data (``~/.grafeno/tasks/<id>/``)
is mirrored to the remote host with rsync over ssh so the history can be
inspected and continued from either side. Everything here is best effort:
sync failures never break the pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import paths
from .i18n import t

if TYPE_CHECKING:
    from .models import Task

MOUNT_TIMEOUT_S = 30
SYNC_TIMEOUT_S = 180
OS_PROBE_TIMEOUT_S = 15

# scp-like: [user@]host:/abs/path (user may contain dots and dashes)
_SCP_LIKE = re.compile(
    r"^(?:(?P<user>[\w.\-]+)@)?(?P<host>[\w.\-]+):(?P<path>/\S*)$"
)
# ssh URL: ssh://[user@]host[:port][/abs/path]
_SSH_URL = re.compile(
    r"^ssh://(?:(?P<user>[\w.\-]+)@)?(?P<host>[\w.\-]+)"
    r"(?::(?P<port>\d+))?(?P<path>/\S*)?$"
)

_SELF_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass
class RemoteSpec:
    """Connection data of a remote project location."""

    user: str = ""
    host: str = ""
    port: int = 0   # 0 = ssh default port
    path: str = ""  # absolute path on the remote host

    @property
    def target(self) -> str:
        """``user@host`` (or just ``host``) for ssh/rsync commands."""
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def canonical(self) -> str:
        """Persisted form: ``user@host:/path`` (port omitted)."""
        return f"{self.target}:{self.path}"

    def to_dict(self) -> dict[str, str | int]:
        return {"user": self.user, "host": self.host, "port": self.port, "path": self.path}

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteSpec":
        return cls(
            user=str(data.get("user", "")),
            host=str(data.get("host", "")),
            port=int(data.get("port", 0)),
            path=str(data.get("path", "")),
        )


def parse_spec(text: str) -> RemoteSpec | None:
    """Parse a remote location; ``None`` when it is not remote-shaped.

    Accepts ``[user@]host:/abs/path`` and ``ssh://[user@]host[:port][/path]``.
    Plain local paths (no ``host:`` part, no ``ssh://`` prefix) return None.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    match = _SSH_URL.match(cleaned) or _SCP_LIKE.match(cleaned)
    if not match:
        return None
    groups = match.groupdict()
    path = groups.get("path") or "/"
    return RemoteSpec(
        user=groups.get("user") or "",
        host=groups.get("host") or "",
        port=int(groups.get("port") or 0),
        path=path.rstrip("/") or "/",
    )


def sshfs_available() -> bool:
    return shutil.which("sshfs") is not None


def rsync_available() -> bool:
    return shutil.which("rsync") is not None


def is_self(spec: RemoteSpec) -> bool:
    """True when the remote host is actually this machine."""
    names = {socket.gethostname(), socket.getfqdn()}
    return spec.host in _SELF_HOSTS or spec.host in names


def mount_dir(spec: RemoteSpec) -> Path:
    """Deterministic local mount point for a spec: ``<slug>-<hash8>``."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", spec.host).strip("-").lower() or "remote"
    digest = hashlib.sha1(spec.canonical.encode("utf-8")).hexdigest()[:8]
    return paths.mounts_dir() / f"{slug}-{digest}"


def effective_workdir(remote_spec: str, workdir: str) -> Path:
    """Local working directory for a (possibly remote) task.

    - empty/invalid spec -> the plain ``workdir`` path;
    - self host          -> the remote path itself (it is local);
    - otherwise          -> the sshfs mount point.
    """
    spec = parse_spec(remote_spec)
    if spec is None:
        return Path(workdir)
    if is_self(spec):
        return Path(spec.path)
    return mount_dir(spec)


def _ssh_args(spec: RemoteSpec) -> list[str]:
    """Extra ssh arguments (only the port, when set)."""
    return ["-p", str(spec.port)] if spec.port else []


async def _run_quiet(command: list[str], timeout: float) -> tuple[int, str]:
    """Run a command capturing output; returns (returncode, stderr tail).

    Never raises: OSError/timeout become returncode -1 with the message.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return -1, str(exc)
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return -1, t("remote.timeout", seconds=int(timeout))
    tail = stderr.decode("utf-8", errors="replace").strip().splitlines()
    return process.returncode or 0, "\n".join(tail[-5:])


async def _run_capture(command: list[str], timeout: float) -> tuple[int, str]:
    """Run a command capturing stdout; returns (returncode, stdout text).

    Never raises: OSError/timeout become returncode -1 with empty output.
    Stderr is discarded: probes only care about the payload.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return -1, ""
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return -1, ""
    return process.returncode or 0, stdout.decode("utf-8", errors="replace")


async def is_mounted(mount_point: Path) -> bool:
    """True if ``mount_point`` appears in the ``mount`` output."""
    completed = await asyncio.create_subprocess_exec(
        "mount", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    stdout, _ = await completed.communicate()
    return str(mount_point) in stdout.decode("utf-8", errors="replace")


async def ensure_mounted(spec: RemoteSpec, on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Mount the remote project with sshfs if needed. True = ready to use."""
    if is_self(spec):
        return True  # the "remote" path is local: nothing to mount
    point = mount_dir(spec)
    if await is_mounted(point):
        return True
    if not sshfs_available():
        on_info(t("remote.no_sshfs"))
        return False
    point.mkdir(parents=True, exist_ok=True)
    command = ["sshfs", f"{spec.target}:{spec.path}", str(point), "-o", "reconnect"]
    if spec.port:
        command += ["-p", str(spec.port)]
    code, error = await _run_quiet(command, MOUNT_TIMEOUT_S)
    if code == 0:
        on_info(t("remote.mount.ok", path=point))
        return True
    on_info(t("remote.mount.fail", error=error or f"exit {code}"))
    return False


def _remote_task_dir(task_id: str) -> str:
    """Remote-side path of the task data directory (for rsync/ssh)."""
    return f"~/.grafeno/tasks/{task_id}"


async def push_task(spec: RemoteSpec, task_id: str, on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Mirror local task data to the remote host (rsync --update)."""
    if is_self(spec):
        return True  # same machine: the data is already there
    if not rsync_available():
        on_info(t("remote.no_rsync"))
        return False
    local = str(paths.task_dir(task_id)) + "/"  # trailing slash: contents
    remote = f"{spec.target}:{_remote_task_dir(task_id)}/"
    await _run_quiet(
        ["ssh", *_ssh_args(spec), spec.target, f"mkdir -p ~/.grafeno/tasks/{task_id}"],
        SYNC_TIMEOUT_S,
    )
    ssh_opt = f"ssh -p {spec.port}" if spec.port else "ssh"
    code, error = await _run_quiet(
        ["rsync", "-az", "--update", "-e", ssh_opt, local, remote], SYNC_TIMEOUT_S
    )
    if code == 0:
        on_info(t("remote.sync.push.ok", target=spec.target))
        return True
    on_info(t("remote.sync.push.fail", error=error or f"exit {code}"))
    return False


async def pull_task(spec: RemoteSpec, task_id: str, on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Mirror remote task data back to the host (rsync --update)."""
    if is_self(spec):
        return True
    if not rsync_available():
        on_info(t("remote.no_rsync"))
        return False
    remote = f"{spec.target}:{_remote_task_dir(task_id)}/"
    local = str(paths.task_dir(task_id)) + "/"
    ssh_opt = f"ssh -p {spec.port}" if spec.port else "ssh"
    code, error = await _run_quiet(
        ["rsync", "-az", "--update", "-e", ssh_opt, remote, local], SYNC_TIMEOUT_S
    )
    if code == 0:
        on_info(t("remote.sync.pull.ok", target=spec.target))
        return True
    on_info(t("remote.sync.pull.fail", error=error or f"exit {code}"))
    return False


async def detect_os(spec: RemoteSpec) -> str:
    """Best-effort probe of the remote OS over ssh ("Linux x86_64", ...).

    Returns "" for self hosts (the CLIs already run on the local OS) and
    on any failure: a missing value must never break the pipeline.
    """
    if is_self(spec):
        return ""
    command = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
        *_ssh_args(spec), spec.target, "uname -srm || ver",
    ]
    code, output = await _run_capture(command, OS_PROBE_TIMEOUT_S)
    if code != 0:
        return ""
    stripped = output.strip()
    if not stripped:
        return ""
    return " ".join(stripped.splitlines()[0].split())[:80]


async def ensure_mounted_for(task: "Task", on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Mount (no-op for local tasks) using the task's remote spec."""
    spec = parse_spec(task.remote)
    if spec is None:
        return True
    return await ensure_mounted(spec, on_info)


async def push_task_for(task: "Task", on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Push task data (no-op for local tasks) using the task's remote spec."""
    spec = parse_spec(task.remote)
    if spec is None:
        return True
    return await push_task(spec, task.id, on_info)


async def pull_task_for(task: "Task", on_info: Callable[[str], None] = lambda m: None) -> bool:
    """Pull task data (no-op for local tasks) using the task's remote spec."""
    spec = parse_spec(task.remote)
    if spec is None:
        return True
    return await pull_task(spec, task.id, on_info)
