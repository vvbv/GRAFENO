"""Direct CLI sessions: conversations with an agent CLI outside the task pipeline.

Each session is bound to a project folder and lives in
``~/.grafeno/cli-sessions/<id>/``: ``session.toml`` (metadata),
``transcript.jsonl`` (user prompts and agent output, best effort) and
``logs/turn-NNN.jsonl`` (raw CLI output of each turn). The running state is
kept only in memory by ``CliSessionManager``; on disk a session is just
``active`` or ``closed``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _toml, paths, remote, remotesession
from .i18n import prompt_template, t

if TYPE_CHECKING:
    from .models import Task

SESSION_CLIS = ("claude", "opencode")  # CLIs offered for direct sessions
STATE_ACTIVE = "active"
STATE_CLOSED = "closed"
STATES = (STATE_ACTIVE, STATE_CLOSED)
ENTRY_KINDS = ("user", "text", "tool", "info", "error")
_META_NAME = "session.toml"
_TRANSCRIPT_NAME = "transcript.jsonl"
_CONTEXT_NAME = "context.md"
_ID_PATTERN = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")
PREVIEW_CHARS = 200  # last_prompt preview length
CONTEXT_MAX_CHARS = 60_000  # summary characters sent as initial context


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class CliSession:
    id: str
    workdir: str              # absolute, resolved local path of the project
    cli: str                  # one of SESSION_CLIS
    name: str = ""
    model: str = ""           # empty = CLI default
    effort: str = ""          # empty = CLI default
    cli_session_id: str = ""  # native session id reported by the CLI (resume key)
    state: str = STATE_ACTIVE  # active | closed (running is only in memory)
    turns: int = 0
    last_prompt: str = ""
    last_error: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: int = 0
    created_at: str = ""
    updated_at: str = ""
    task_id: str = ""         # task the session was opened from (initial context); empty = none

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CliSession":
        state = str(data.get("state", STATE_ACTIVE))
        return cls(
            id=str(data.get("id", "")),
            workdir=str(data.get("workdir", "")),
            cli=str(data.get("cli", "")),
            name=str(data.get("name", "")),
            model=str(data.get("model", "")),
            effort=str(data.get("effort", "")),
            cli_session_id=str(data.get("cli_session_id", "")),
            state=state if state in STATES else STATE_ACTIVE,
            turns=_to_int(data.get("turns")),
            last_prompt=str(data.get("last_prompt", "")),
            last_error=str(data.get("last_error", "")),
            tokens_in=_to_int(data.get("tokens_in")),
            tokens_out=_to_int(data.get("tokens_out")),
            seconds=_to_int(data.get("seconds")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            task_id=str(data.get("task_id", "")),
        )

    @property
    def project_name(self) -> str:
        return Path(self.workdir).name or self.workdir


def new_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def valid_id(session_id: str) -> bool:
    """Whether the id has the generated shape (protects against path traversal)."""
    return bool(_ID_PATTERN.fullmatch(session_id or ""))


def resolve_workdir(raw: str) -> Path:
    """Validate and resolve a local project folder; ``ValueError`` when invalid."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("workdir is required")
    if remote.parse_spec(text):
        raise ValueError("remote projects are not supported")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("workdir must be an absolute path")
    if not path.is_dir():
        raise ValueError("workdir not found")
    return path.resolve()


def create(
    workdir: str, cli: str, model: str = "", effort: str = "", name: str = "",
    *, task_id: str = "",
) -> CliSession:
    """Create and persist a new active session in a project folder."""
    if cli not in SESSION_CLIS:
        raise ValueError("unsupported cli")
    if remotesession.active():
        raise ValueError("not available in remote session mode")
    path = resolve_workdir(workdir)
    name = name.strip() or t("cs.default_name", n=len(list_all(str(path))) + 1)
    session = CliSession(
        id=new_id(),
        workdir=str(path),
        cli=cli,
        name=name,
        model=model.strip(),
        effort=effort.strip(),
        created_at=_now(),
        task_id=task_id,
    )
    save(session)
    return session


_TASK_CONTEXT = {
    "es": (
        "Contexto inicial de esta sesion: proviene de la tarea de GRAFENO \"{task_name}\" "
        "(id {task_id}) del proyecto {workdir}. Complementa las instrucciones del proyecto "
        "(AGENTS.md, CLAUDE.md...) que ya cargas por tu cuenta.\n"
        "Los artefactos completos de la tarea (plan, revision, informe final, changes.md) "
        "estan en {task_dir} por si necesitas mas detalle.\n\n"
        "{source}:\n{body}\n\n"
        "---\n"
        "Primer mensaje del usuario:\n"
    ),
    "en": (
        "Initial context of this session: it comes from the GRAFENO task \"{task_name}\" "
        "(id {task_id}) of the project {workdir}. It complements the project instructions "
        "(AGENTS.md, CLAUDE.md...) that you already load on your own.\n"
        "The full artifacts of the task (plan, review, final report, changes.md) are in "
        "{task_dir} if you need more detail.\n\n"
        "{source}:\n{body}\n\n"
        "---\n"
        "First user message:\n"
    ),
}
_SOURCE_SUMMARY = {"es": "Resumen de la tarea ({path})", "en": "Task summary ({path})"}
_SOURCE_DESCRIPTION = {
    "es": "La tarea aun no tiene summary.md; descripcion de la tarea",
    "en": "The task has no summary.md yet; task description",
}
_TRUNCATED_CONTEXT = {"es": "(resumen truncado)", "en": "(summary truncated)"}


def context_path(session_id: str) -> Path:
    return paths.cli_session_dir(session_id) / _CONTEXT_NAME


def save_initial_context(session_id: str, text: str) -> None:
    """Store the context sent with the next (first) turn of the session."""
    path = context_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_initial_context(session_id: str) -> str:
    """Pending initial context, or "" when there is none (or it is unreadable)."""
    try:
        return context_path(session_id).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def clear_initial_context(session_id: str) -> None:
    try:
        context_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


def has_initial_context(session_id: str) -> bool:
    return context_path(session_id).is_file()


def latest_summary(task: "Task") -> Path | None:
    """summary.md of the most recent cycle that has one."""
    for cycle in range(max(task.cycle, 1), 0, -1):
        path = paths.final_dir(task.id, cycle) / "summary.md"
        if path.is_file():
            return path
    return None


def task_context(task: "Task") -> str:
    """Initial context block for a session opened from ``task`` (ends ready for the user text)."""
    summary = latest_summary(task)
    body = ""
    if summary is not None:
        try:
            body = summary.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            body = ""
    if summary is not None and body:
        source = prompt_template(_SOURCE_SUMMARY).format(path=str(summary))
    else:
        body = task.description.strip() or "-"
        source = prompt_template(_SOURCE_DESCRIPTION)
    if len(body) > CONTEXT_MAX_CHARS:
        body = body[:CONTEXT_MAX_CHARS] + "\n" + prompt_template(_TRUNCATED_CONTEXT)
    return prompt_template(_TASK_CONTEXT).format(  # formatted once: braces in the body stay literal
        task_name=task.name, task_id=task.id, workdir=task.workdir,
        task_dir=str(paths.task_dir(task.id)), source=source, body=body,
    )


def save(session: CliSession) -> None:
    session.updated_at = _now()
    folder = paths.cli_session_dir(session.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _META_NAME).write_text(_toml.dumps(session.to_dict()), encoding="utf-8")


def load(session_id: str) -> CliSession:
    if not valid_id(session_id):
        raise FileNotFoundError(session_id)
    meta = paths.cli_session_dir(session_id) / _META_NAME
    with meta.open("rb") as handle:
        return CliSession.from_dict(tomllib.load(handle))


def _same_dir(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def list_all(workdir: str = "") -> list[CliSession]:
    """All sessions (optionally of one project), most recently updated first."""
    target = _same_dir(workdir) if workdir else ""
    sessions: list[CliSession] = []
    for folder in paths.cli_sessions_dir().iterdir():
        if not folder.is_dir() or not valid_id(folder.name):
            continue
        try:
            session = load(folder.name)
        except (OSError, ValueError):
            continue  # corrupt, binary or half-written session: skipped (TOMLDecodeError and UnicodeDecodeError are ValueErrors)
        if target and _same_dir(session.workdir) != target:
            continue
        sessions.append(session)
    return sorted(sessions, key=lambda s: s.updated_at, reverse=True)


def delete(session_id: str) -> bool:
    if not valid_id(session_id):
        return False
    folder = paths.cli_session_dir(session_id)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    return True


def append_entry(session_id: str, turn: int, kind: str, text: str) -> None:
    """Append one transcript entry (best effort: never raises)."""
    if not valid_id(session_id):
        return
    if kind not in ENTRY_KINDS:
        kind = "info"
    record = {"turn": turn, "kind": kind, "text": text, "ts": _now()}
    try:
        folder = paths.cli_session_dir(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / _TRANSCRIPT_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass  # the transcript is a convenience; a turn never fails because of it


def load_transcript(
    session_id: str, after: int = 0, limit: int = 0
) -> tuple[list[dict], int]:
    """Transcript entries after ``after`` (global 1-based ``seq``) and the total."""
    if not valid_id(session_id):
        return [], 0
    path = paths.cli_session_dir(session_id) / _TRANSCRIPT_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], 0
    entries: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            entries.append(record)
    total = len(entries)
    for index, entry in enumerate(entries, start=1):
        entry["seq"] = index  # global index, computed before slicing
    rest = entries[max(0, after):]
    if limit > 0:
        rest = rest[-limit:]
    return rest, total


def turn_log_path(session_id: str, turn: int) -> Path:
    """Raw JSONL log of one turn: ``logs/turn-001.jsonl``."""
    logs = paths.cli_session_dir(session_id) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / f"turn-{turn:03d}.jsonl"
