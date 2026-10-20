"""Assisted review sessions of pending git changes across nested repositories.

Data lives in ``~/.grafeno/reviews/<id>/``: ``review.toml`` (metadata and
per-file decisions) and ``commits.jsonl`` (history of commits made from the
review). The git state is never stored: it is recomputed on every snapshot.
Nothing is committed unless ``commit_files`` is called explicitly.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import _toml, clisessions, gitrepos, paths, remotesession, usage
from .clisession_runtime import CliSessionManager
from .clisessions import SESSION_CLIS, CliSession, new_id, valid_id
from .drivers import get_driver
from .drivers.base import RunRequest
from .i18n import prompt_template, t

STATE_ACTIVE = "active"
STATE_CLOSED = "closed"
STATES = (STATE_ACTIVE, STATE_CLOSED)
DECISIONS = ("pending", "accepted", "changes_requested", "skipped")
NAV_ACTIONS = ("first", "next", "prev", "goto", "current")
PROMPT_DIFF_BYTES = 64 * 1024      # diff embedded in an agent prompt
COMMIT_MESSAGE_TIMEOUT = 180.0     # seconds
COMMIT_MESSAGE_DIFF_BYTES = 48 * 1024
_META_NAME = "review.toml"
_COMMITS_NAME = "commits.jsonl"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass
class FileDecision:
    repo: str
    path: str                 # repo-relative
    decision: str = "pending"  # one of DECISIONS
    note: str = ""            # user note / requested changes
    fingerprint: str = ""     # gitrepos.fingerprint when decided (detects later edits)
    discussions: int = 0      # agent turns about this file
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "path": self.path, "decision": self.decision,
            "note": self.note, "fingerprint": self.fingerprint,
            "discussions": self.discussions, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FileDecision":
        decision = str(data.get("decision", "pending"))
        return cls(
            repo=str(data.get("repo", "")),
            path=str(data.get("path", "")),
            decision=decision if decision in DECISIONS else "pending",
            note=str(data.get("note", "")),
            fingerprint=str(data.get("fingerprint", "")),
            discussions=_to_int(data.get("discussions")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class ReviewSession:
    id: str
    workdir: str              # resolved local project folder
    name: str = ""
    task_id: str = ""         # optional task this review comes from (informative)
    state: str = STATE_ACTIVE
    cli: str = "claude"       # agent CLI (SESSION_CLIS)
    model: str = ""
    effort: str = ""
    agent_session_id: str = ""  # CliSession.id of the agent conversation ("" = not created yet)
    cursor_repo: str = ""
    cursor_path: str = ""
    commits: int = 0
    files: list[FileDecision] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "workdir": self.workdir, "name": self.name,
            "task_id": self.task_id, "state": self.state, "cli": self.cli,
            "model": self.model, "effort": self.effort,
            "agent_session_id": self.agent_session_id,
            "cursor_repo": self.cursor_repo, "cursor_path": self.cursor_path,
            "commits": self.commits, "created_at": self.created_at,
            "updated_at": self.updated_at,
            "files": [item.to_dict() for item in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewSession":
        state = str(data.get("state", STATE_ACTIVE))
        raw_files = data.get("files") or []
        files = [FileDecision.from_dict(item) for item in raw_files if isinstance(item, dict)]
        return cls(
            id=str(data.get("id", "")),
            workdir=str(data.get("workdir", "")),
            name=str(data.get("name", "")),
            task_id=str(data.get("task_id", "")),
            state=state if state in STATES else STATE_ACTIVE,
            cli=str(data.get("cli", "claude")) or "claude",
            model=str(data.get("model", "")),
            effort=str(data.get("effort", "")),
            agent_session_id=str(data.get("agent_session_id", "")),
            cursor_repo=str(data.get("cursor_repo", "")),
            cursor_path=str(data.get("cursor_path", "")),
            commits=_to_int(data.get("commits")),
            files=files,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def decision_for(self, repo: str, path: str) -> FileDecision | None:
        for item in self.files:
            if item.repo == repo and item.path == path:
                return item
        return None


# --- store -------------------------------------------------------------------

def create(
    workdir: str, *, name: str = "", task_id: str = "", cli: str = "claude",
    model: str = "", effort: str = "",
) -> ReviewSession:
    """Create and persist a new active review of a local project folder."""
    if cli not in SESSION_CLIS:
        raise ValueError("unsupported cli")
    if remotesession.active():
        raise ValueError("not available in remote session mode")
    path = clisessions.resolve_workdir(workdir)
    name = name.strip() or t("rv.default_name", n=len(list_all(str(path))) + 1)
    review = ReviewSession(
        id=new_id(), workdir=str(path), name=name, task_id=task_id.strip(),
        cli=cli, model=model.strip(), effort=effort.strip(), created_at=_now(),
    )
    save(review)
    return review


def save(review: ReviewSession) -> None:
    review.updated_at = _now()
    folder = paths.review_session_dir(review.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _META_NAME).write_text(_toml.dumps(review.to_dict()), encoding="utf-8")


def load(review_id: str) -> ReviewSession:
    if not valid_id(review_id):
        raise FileNotFoundError(review_id)
    with (paths.review_session_dir(review_id) / _META_NAME).open("rb") as handle:
        return ReviewSession.from_dict(tomllib.load(handle))


def _same_dir(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def list_all(workdir: str = "", state: str = "") -> list[ReviewSession]:
    """All reviews (optionally of one project and state), most recently updated first."""
    target = _same_dir(workdir) if workdir else ""
    reviews: list[ReviewSession] = []
    for folder in paths.review_sessions_dir().iterdir():
        if not folder.is_dir() or not valid_id(folder.name):
            continue
        try:
            review = load(folder.name)
        except (OSError, ValueError):
            continue  # corrupt or half-written review: skipped
        if target and _same_dir(review.workdir) != target:
            continue
        if state and review.state != state:
            continue
        reviews.append(review)
    return sorted(reviews, key=lambda r: r.updated_at, reverse=True)


def delete(review_id: str) -> bool:
    if not valid_id(review_id):
        return False
    folder = paths.review_session_dir(review_id)
    if not folder.is_dir():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    _LOCKS.pop(review_id, None)
    return True


_LOCKS: dict[str, asyncio.Lock] = {}


def review_lock(review_id: str) -> asyncio.Lock:
    """Per-review lock that serializes load -> mutate -> save sequences."""
    lock = _LOCKS.get(review_id)
    if lock is None:
        lock = _LOCKS[review_id] = asyncio.Lock()
    return lock


def find_active(workdir: str, task_id: str = "") -> ReviewSession | None:
    """Most recent active review of the folder (and of the task when given)."""
    for review in list_all(workdir, STATE_ACTIVE):
        if task_id and review.task_id != task_id:
            continue
        return review
    return None


def append_commit(review_id: str, record: dict) -> None:
    """Append one commit record (best effort: never raises)."""
    if not valid_id(review_id):
        return
    entry = dict(record)
    entry["ts"] = _now()
    try:
        folder = paths.review_session_dir(review_id)
        if not folder.is_dir():
            return  # deleted meanwhile: never resurrect the folder
        with (folder / _COMMITS_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_commits(review_id: str) -> list[dict]:
    if not valid_id(review_id):
        return []
    try:
        lines = (paths.review_session_dir(review_id) / _COMMITS_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# --- snapshot ----------------------------------------------------------------

@dataclass
class Snapshot:
    root: Path
    repos: list[gitrepos.Repo]
    statuses: dict[str, gitrepos.RepoStatus]   # by repo id
    errors: dict[str, str]                      # repo id -> git error message (repo skipped)
    entries: list[dict]                         # ordered file entries


async def snapshot(review: ReviewSession) -> Snapshot:
    """Current git state of every repository of the project merged with the decisions."""
    if not gitrepos.git_available():
        raise gitrepos.GitReviewError("git_missing", "git is not installed")
    root = Path(review.workdir)
    repos = await asyncio.to_thread(gitrepos.discover, root)
    results = await asyncio.gather(*(gitrepos.status(r) for r in repos), return_exceptions=True)
    statuses: dict[str, gitrepos.RepoStatus] = {}
    errors: dict[str, str] = {}
    for repo, result in zip(repos, results):
        if isinstance(result, gitrepos.GitReviewError):
            errors[repo.id] = result.message
        elif isinstance(result, BaseException):
            raise result
        else:
            statuses[repo.id] = result
    entries: list[dict] = []
    for repo in repos:
        st = statuses.get(repo.id)
        if st is None:
            continue
        for change in st.files:
            decision = review.decision_for(repo.id, change.path)
            current = gitrepos.fingerprint(repo, change)
            decided = decision is not None and decision.decision != "pending"
            entries.append({
                "key": gitrepos.file_key(repo.id, change.path),
                "repo": repo.id, "repo_path": str(repo.path),
                "path": change.path,
                "project_path": gitrepos.project_path(repo, change.path),
                "abs_path": str(repo.path / change.path),
                "orig_path": change.orig_path,
                "kind": change.kind, "index": change.index, "worktree": change.worktree,
                "staged": change.staged, "unstaged": change.unstaged,
                "partially_staged": change.partially_staged,
                "committable": change.committable,
                "fingerprint": current,
                "decision": decision.decision if decision else "pending",
                "note": decision.note if decision else "",
                "discussions": decision.discussions if decision else 0,
                "outdated": bool(decided and decision.fingerprint != current),
                "position": len(entries) + 1,
            })
    return Snapshot(root, repos, statuses, errors, entries)


def find_entry(snap: Snapshot, repo: str, path: str) -> dict | None:
    for entry in snap.entries:
        if entry["repo"] == repo and entry["path"] == path:
            return entry
    return None


def repo_by_id(snap: Snapshot, repo_id: str) -> gitrepos.Repo | None:
    for repo in snap.repos:
        if repo.id == repo_id:
            return repo
    return None


def change_of(snap: Snapshot, entry: dict) -> gitrepos.FileChange:
    """The ``FileChange`` behind a snapshot entry."""
    for change in snap.statuses[entry["repo"]].files:
        if change.path == entry["path"]:
            return change
    raise ValueError("file not pending")


def prune(review: ReviewSession, snap: Snapshot) -> bool:
    """Drop decisions of files that are no longer pending; True when something changed."""
    pending = {(e["repo"], e["path"]) for e in snap.entries}
    kept = [d for d in review.files if (d.repo, d.path) in pending]
    changed = len(kept) != len(review.files)
    review.files = kept
    return changed


def counts(snap: Snapshot) -> dict[str, int]:
    """Totals by effective decision (an outdated decision counts as pending too)."""
    result = {"total": len(snap.entries), "pending": 0, "accepted": 0,
              "changes_requested": 0, "skipped": 0, "outdated": 0,
              "repos": len(snap.repos),
              "repos_with_changes": len({e["repo"] for e in snap.entries})}
    for entry in snap.entries:
        if entry["outdated"]:
            result["outdated"] += 1
            result["pending"] += 1
        else:
            result[entry["decision"]] += 1
    return result


# --- navigation ----------------------------------------------------------------

def _is_open(entry: dict) -> bool:
    """Effectively pending: undecided or decided on a version that changed since."""
    return entry["decision"] == "pending" or entry["outdated"]


def _repo_order(snap: Snapshot) -> dict[str, int]:
    return {repo.id: index for index, repo in enumerate(snap.repos)}


def _current_index(review: ReviewSession, snap: Snapshot) -> int | None:
    """Index of the cursor entry; when it vanished, the first one after its old position."""
    if not snap.entries:
        return None
    for index, entry in enumerate(snap.entries):
        if entry["repo"] == review.cursor_repo and entry["path"] == review.cursor_path:
            return index
    if not review.cursor_path:
        return 0
    order = _repo_order(snap)
    saved = (order.get(review.cursor_repo, len(order)), review.cursor_path.casefold())
    for index, entry in enumerate(snap.entries):
        if (order.get(entry["repo"], len(order)), entry["path"].casefold()) > saved:
            return index
    return 0


def _cursor_exact(review: ReviewSession, snap: Snapshot) -> bool:
    """True when the stored cursor still points at a pending file."""
    return bool(review.cursor_path) and find_entry(snap, review.cursor_repo, review.cursor_path) is not None


def navigate(review: ReviewSession, snap: Snapshot, action: str,
             repo: str = "", path: str = "", skip_decided: bool = False) -> dict | None:
    """Move the cursor and return the entry under it (None when there are no files)."""
    if action not in NAV_ACTIONS:
        raise ValueError("unsupported action")
    entries = snap.entries
    target: dict | None
    if action == "goto":
        if not repo or not path:
            raise ValueError("repo and path are required")
        target = find_entry(snap, repo, path)
        if target is None:
            raise ValueError("file not pending")
    elif action == "current":
        index = _current_index(review, snap)
        target = entries[index] if index is not None else None
    elif action == "first":
        candidates = [e for e in entries if _is_open(e)] if skip_decided else entries
        target = candidates[0] if candidates else None
    else:
        index = _current_index(review, snap)
        if index is None:
            return None
        step = 1 if action == "next" else -1
        target = None
        position = index + step
        if not _cursor_exact(review, snap):
            # the cursor file vanished (e.g. it was just committed): the entry that
            # took its place is already the "next" one, so do not skip it
            position = index if step == 1 else index - 1
        while 0 <= position < len(entries):
            if not skip_decided or _is_open(entries[position]):
                target = entries[position]
                break
            position += step
        if target is None:
            return None  # end reached: no wrap-around, cursor stays
    if target is not None:
        review.cursor_repo = target["repo"]
        review.cursor_path = target["path"]
    return target


# --- decisions -----------------------------------------------------------------

def decide(review: ReviewSession, snap: Snapshot, repo: str, path: str,
           decision: str, note: str = "") -> FileDecision:
    """Record the decision for a pending file."""
    if decision not in DECISIONS:
        raise ValueError("unsupported decision")
    entry = find_entry(snap, repo, path)
    if entry is None:
        raise ValueError("file not pending")
    item = review.decision_for(repo, path)
    if item is None:
        item = FileDecision(repo=repo, path=path)
        review.files.append(item)
    item.decision = decision
    if decision == "pending":
        item.note = note
    else:
        item.note = note or item.note
    item.fingerprint = entry["fingerprint"]
    item.updated_at = _now()
    entry["decision"] = decision
    entry["note"] = item.note
    entry["outdated"] = False
    return item


# --- agent ---------------------------------------------------------------------

_FILE_SECTION = {
    "es": (
        "Archivo en revisión:\n"
        "- Repositorio: {repo_id} (ruta absoluta: {repo_path})\n"
        "- Archivo: {file_path} (relativo a ese repositorio; relativo al proyecto: {project_path})\n"
        "- Tipo de cambio: {kind}\n"
        "El archivo pertenece al repositorio {repo_path} y no al de la raíz del proyecto.\n\n"
        "Diff:\n{diff}\n"
    ),
    "en": (
        "File under review:\n"
        "- Repository: {repo_id} (absolute path: {repo_path})\n"
        "- File: {file_path} (relative to that repository; relative to the project: {project_path})\n"
        "- Change kind: {kind}\n"
        "The file belongs to the repository {repo_path}, not to the project root repository.\n\n"
        "Diff:\n{diff}\n"
    ),
}

_DISCUSS_PROMPT = {
    "es": (
        "Estas revisando cambios pendientes (sin commit) de la revision \"{review_name}\" "
        "en el proyecto {workdir}.\n\n"
        "{file_section}\n"
        "Pregunta o comentario del usuario:\n{message}\n\n"
        "Reglas:\n"
        "- Responde la pregunta o comenta el cambio.\n"
        "- NO modifiques archivos.\n"
        "- NO ejecutes comandos git que cambien estado (add, commit, reset, checkout, stash, "
        "restore, rebase, merge, push).\n"
    ),
    "en": (
        "You are reviewing pending (uncommitted) changes for the review \"{review_name}\" "
        "in the project {workdir}.\n\n"
        "{file_section}\n"
        "User question or comment:\n{message}\n\n"
        "Rules:\n"
        "- Answer the question or comment on the change.\n"
        "- Do NOT modify any file.\n"
        "- Do NOT run git commands that change state (add, commit, reset, checkout, stash, "
        "restore, rebase, merge, push).\n"
    ),
}

_CHANGES_PROMPT = {
    "es": (
        "Revision \"{review_name}\" del proyecto {workdir}: el usuario pide cambios sobre un archivo "
        "con modificaciones pendientes.\n\n"
        "{file_section}\n"
        "Cambios solicitados:\n{instructions}\n\n"
        "Reglas:\n"
        "- Aplica los cambios pedidos SOLO en {file_path} del repositorio {repo_path}. Toca otros "
        "archivos del MISMO repositorio solo si es imprescindible y dilo explicitamente.\n"
        "- No toques otros repositorios.\n"
        "- NO hagas stage, commit, stash, reset ni checkout: el usuario revisara y comiteara.\n"
        "- Termina con un resumen breve de lo cambiado.\n"
    ),
    "en": (
        "Review \"{review_name}\" of the project {workdir}: the user requests changes on a file "
        "with pending modifications.\n\n"
        "{file_section}\n"
        "Requested changes:\n{instructions}\n\n"
        "Rules:\n"
        "- Apply the requested changes ONLY in {file_path} of the repository {repo_path}. Touch "
        "other files of the SAME repository only when strictly necessary and say so explicitly.\n"
        "- Do not touch other repositories.\n"
        "- Do NOT stage, commit, stash, reset or checkout: the user will review and commit.\n"
        "- End with a short summary of what changed.\n"
    ),
}

_COMMIT_MESSAGE_PROMPT = {
    "es": (
        "Escribe el mensaje de commit para los cambios pendientes del repositorio {repo_path}.\n\n"
        "Archivos:\n{files}\n\n"
        "Ultimos commits del repositorio:\n{log}\n\n"
        "Pista del usuario: {hint}\n\n"
        "Diff:\n{diff}\n\n"
        "Reglas:\n"
        "- Devuelve SOLO el mensaje de commit, sin comillas ni bloques de codigo.\n"
        "- Primera linea de 72 caracteres como maximo, en imperativo.\n"
        "- Sigue el estilo de los ultimos commits (p. ej. Conventional Commits si el historial lo "
        "usa) y el AGENTS.md del repositorio si existe.\n"
        "- Escribelo en INGLES salvo que el AGENTS.md o la pista del usuario digan lo contrario.\n"
        "- No ejecutes comandos ni modifiques archivos.\n"
    ),
    "en": (
        "Write the commit message for the pending changes of the repository {repo_path}.\n\n"
        "Files:\n{files}\n\n"
        "Recent commits of the repository:\n{log}\n\n"
        "User hint: {hint}\n\n"
        "Diff:\n{diff}\n\n"
        "Rules:\n"
        "- Return ONLY the commit message, without quotes or code blocks.\n"
        "- First line of at most 72 characters, imperative mood.\n"
        "- Follow the style of the recent commits (e.g. Conventional Commits when the history "
        "uses it) and the repository AGENTS.md if present.\n"
        "- Write it in ENGLISH unless the AGENTS.md or the user hint say otherwise.\n"
        "- Do not run commands or modify files.\n"
    ),
}

_TRUNCATED = {"es": "(diff truncado)", "en": "(diff truncated)"}


def ensure_agent(manager: CliSessionManager, review: ReviewSession) -> CliSession:
    """The direct CLI session of the review, created lazily and reopened when closed."""
    if review.agent_session_id:
        try:
            session = manager.get(review.agent_session_id)
        except (FileNotFoundError, OSError, ValueError):
            session = None  # deleted from elsewhere (e.g. the TUI)
        if session is not None:
            if session.state == clisessions.STATE_CLOSED:
                session = manager.reopen(session.id)
            return session
    session = manager.create(
        review.workdir, review.cli, model=review.model, effort=review.effort,
        name=t("rv.agent_name", name=review.name),
    )
    review.agent_session_id = session.id
    save(review)
    return session


async def _file_section(snap: Snapshot, entry: dict) -> str:
    repo = repo_by_id(snap, entry["repo"])
    if repo is None:
        raise ValueError("file not pending")
    change = change_of(snap, entry)
    has_head = bool(snap.statuses[entry["repo"]].head)
    file_diff = await gitrepos.diff(repo, change, has_head, max_bytes=PROMPT_DIFF_BYTES,
                                    split=False)
    text = file_diff.text
    if file_diff.truncated:
        text += "\n" + prompt_template(_TRUNCATED)
    return prompt_template(_FILE_SECTION).format(
        repo_id=repo.id, repo_path=str(repo.path), file_path=entry["path"],
        project_path=entry["project_path"], kind=entry["kind"], diff=text,
    )


def _note_discussion(review: ReviewSession, entry: dict) -> FileDecision:
    item = review.decision_for(entry["repo"], entry["path"])
    if item is None:
        item = FileDecision(repo=entry["repo"], path=entry["path"],
                            fingerprint=entry["fingerprint"])
        review.files.append(item)
    item.discussions += 1
    item.updated_at = _now()
    return item


async def discuss(manager: CliSessionManager, review: ReviewSession, snap: Snapshot,
                  entry: dict | None, message: str) -> CliSession:
    """Send a question about the review (or one file) to the agent session."""
    if not message.strip():
        raise ValueError("message is required")
    section = await _file_section(snap, entry) if entry else ""
    session = ensure_agent(manager, review)
    prompt = prompt_template(_DISCUSS_PROMPT).format(
        review_name=review.name, workdir=review.workdir, file_section=section,
        message=message.strip(),
    )
    session = manager.send(session.id, prompt)
    if entry:
        _note_discussion(review, entry)
    save(review)
    return session


async def request_changes(manager: CliSessionManager, review: ReviewSession, snap: Snapshot,
                          entry: dict, instructions: str) -> CliSession:
    """Mark the file as ``changes_requested`` and ask the agent to apply the changes."""
    if not instructions.strip():
        raise ValueError("instructions are required")
    section = await _file_section(snap, entry)
    session = ensure_agent(manager, review)
    prompt = prompt_template(_CHANGES_PROMPT).format(
        review_name=review.name, workdir=review.workdir, file_section=section,
        instructions=instructions.strip(), file_path=entry["path"],
        repo_path=entry["repo_path"],
    )
    session = manager.send(session.id, prompt)
    decide(review, snap, entry["repo"], entry["path"], "changes_requested", instructions.strip())
    save(review)
    return session


# --- assisted commit message -----------------------------------------------------

_FENCE = re.compile(r"^```[A-Za-z0-9_-]*\s*$")
_PREFIX = re.compile(r"^(commit message|mensaje de commit)\s*:\s*", re.IGNORECASE)


def clean_commit_message(text: str) -> str:
    """Strip code fences, wrapping quotes and a leading label from agent output."""
    lines = [line for line in (text or "").replace("\r\n", "\n").split("\n")
             if not _FENCE.match(line.strip())]
    result = "\n".join(lines).strip()
    if len(result) >= 2 and result[0] == result[-1] and result[0] in "\"'`":
        result = result[1:-1].strip()
    result = _PREFIX.sub("", result).strip()
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


async def suggest_commit_message(review: ReviewSession, snap: Snapshot, repo_id: str,
                                 paths_: list[str], *, hint: str = "", cli: str = "",
                                 model: str = "", effort: str = "",
                                 timeout: float = COMMIT_MESSAGE_TIMEOUT,
                                 driver_factory=None) -> dict:
    """One-shot agent run that proposes a commit message; it never commits."""
    repo = repo_by_id(snap, repo_id)
    if repo is None or repo_id not in snap.statuses:
        raise gitrepos.GitReviewError("not_a_repo", "repository not found", repo_id)
    status = snap.statuses[repo_id]
    selected = gitrepos.select_changes(repo, status, paths_, include_unstaged=True)
    try:
        log = (await gitrepos.run_git(repo.path, "log", "--oneline", "-10")).decode("utf-8", "replace")
    except gitrepos.GitReviewError:
        log = ""  # unborn branch
    has_head = bool(status.head)
    chunks: list[str] = []
    remaining = COMMIT_MESSAGE_DIFF_BYTES
    for change in selected:
        if remaining <= 0:
            break
        file_diff = await gitrepos.diff(repo, change, has_head, max_bytes=max(remaining, 1024),
                                        split=False)
        chunks.append(file_diff.text)
        remaining -= len(file_diff.text.encode("utf-8"))
    try:
        driver = (driver_factory or get_driver)(cli or review.cli)
    except KeyError as exc:
        raise ValueError("unknown cli") from exc
    if not driver.is_available():
        raise ValueError("cli not available")
    prompt = prompt_template(_COMMIT_MESSAGE_PROMPT).format(
        repo_path=str(repo.path),
        files="\n".join(f"- {c.path} ({c.kind})" for c in selected),
        log=log.strip() or "(none)", diff="\n".join(chunks), hint=hint.strip() or "(none)",
    )
    chosen_model = model or (review.model if not cli or cli == review.cli else "")
    request = RunRequest(
        prompt=prompt, model=chosen_model, workdir=repo.path,
        title=f"grafeno:review:{review.id}:commit-message", effort=effort,
    )
    started = time.monotonic()
    result = await asyncio.wait_for(driver.run(request), timeout)
    seconds = int(time.monotonic() - started)
    usage.record_session(review.workdir, driver.name, request.model,
                         result.tokens.input, result.tokens.output, seconds)
    message = clean_commit_message(result.text) if result.ok else ""
    if not result.ok or not message:
        raise RuntimeError(result.error or "empty output")
    return {"message": message, "repo": repo_id, "files": [c.path for c in selected],
            "cli": driver.name, "model": request.model}


# --- explicit commit ---------------------------------------------------------------

def _resolve_refs(snap: Snapshot, files: list[dict]) -> list[tuple[str, str, str]]:
    """``(repo, path, fingerprint)`` of each requested file, in order, without duplicates."""
    refs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("invalid file reference")
        repo, path = str(item.get("repo") or ""), str(item.get("path") or "")
        if item.get("key"):
            match = next((e for e in snap.entries if e["key"] == item["key"]), None)
            if match is None:
                raise gitrepos.GitReviewError("not_pending", "file is not pending", str(item["key"]))
            repo, path = match["repo"], match["path"]
        if not repo or not path:
            raise ValueError("repo and path are required")
        if (repo, path) in seen:
            continue
        seen.add((repo, path))
        refs.append((repo, path, str(item.get("fingerprint") or "")))
    return refs


async def _merge_after_commit(review: ReviewSession, made: int,
                              snap: Snapshot) -> ReviewSession | None:
    """Apply a commit's effects to a fresh copy of the review (None when it was deleted).

    Commits can take long (hooks) and other requests may have changed the review
    meanwhile, so the stale in-memory copy is never saved.
    """
    async with review_lock(review.id):
        try:
            fresh = load(review.id)
        except (OSError, ValueError):
            return None
        fresh.commits += made
        prune(fresh, snap)
        save(fresh)
        return fresh


async def commit_files(review: ReviewSession, snap: Snapshot, files: list[dict], message: str,
                       *, include_unstaged: bool = False, allow_multi_repo: bool = False) -> dict:
    """Commit the given files, each in its owning repository; the only place that commits."""
    if not files:
        raise ValueError("files is required")
    refs = _resolve_refs(snap, files)
    groups: dict[str, list[tuple[str, str]]] = {}
    for repo_id, path, fp in refs:
        groups.setdefault(repo_id, []).append((path, fp))
    if len(groups) > 1 and not allow_multi_repo:
        raise gitrepos.GitReviewError(
            "multi_repo", "files belong to several repositories: " + ", ".join(groups))
    # validate every group before the first commit
    for repo_id, items in groups.items():
        repo = repo_by_id(snap, repo_id)
        if repo is None or repo_id not in snap.statuses:
            raise gitrepos.GitReviewError("not_a_repo", "repository not found", repo_id)
        gitrepos.select_changes(
            repo, snap.statuses[repo_id], [p for p, _ in items],
            include_unstaged=include_unstaged,
            expected={p: fp for p, fp in items if fp},
        )
    logged = gitrepos.normalize_message(message)  # the exact text git receives
    commits: list[dict] = []
    errors: list[dict] = []
    for repo_id, items in groups.items():
        repo = repo_by_id(snap, repo_id)
        assert repo is not None
        try:
            result = await gitrepos.commit(
                repo, [p for p, _ in items], message, include_unstaged=include_unstaged,
                expected={p: fp for p, fp in items if fp},
            )
        except gitrepos.GitReviewError as exc:
            errors.append({"repo": repo_id, "code": exc.code, "message": exc.message,
                           "path": exc.path})
            break
        record = {"repo": repo_id, "hash": result.hash, "files": result.files,
                  "message": logged}
        commits.append(record)
        append_commit(review.id, record)
    new_snap = await snapshot(review)
    merged = await _merge_after_commit(review, len(commits), new_snap)
    if merged is not None:
        review.__dict__.update(merged.__dict__)  # callers keep using their object
    return {"commits": commits, "errors": errors, "partial": bool(errors) and bool(commits),
            "snapshot": new_snap}
