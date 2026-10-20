"""Multi-repository git core for assisted review: discovery, status, per-file diff and explicit commits.

Everything here is read-only except ``commit``, which records ONLY the paths
it is given (``git commit --only``), never uses ``-a``/``add -A`` and keeps the
staging area of every other path untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .pipeline.gitops import _MAX_DIFF_BYTES
from .projectsetup import _git_env, _tail, git_available

__all__ = [
    "GitReviewError", "Repo", "FileChange", "RepoStatus", "FileDiff", "CommitResult",
    "run_git", "discover", "status", "parse_status_v2", "project_path", "file_key",
    "fingerprint", "validate_rel_path", "diff", "commit", "select_changes", "normalize_message",
    "repo_lock", "git_available",
]

GIT_TIMEOUT = 30.0          # seconds per read command
COMMIT_TIMEOUT = 120.0      # seconds (commit hooks may run)
MAX_DEPTH = 8               # directory levels scanned below the project root
MAX_REPOS = 200
MAX_FILES_PER_REPO = 2000
DIFF_DEFAULT_BYTES = _MAX_DIFF_BYTES
DIFF_MAX_BYTES = 4 * 1024 * 1024
SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".tox", ".godot", "dist", "build",
})
KINDS = ("modified", "added", "deleted", "renamed", "copied", "untracked",
         "conflicted", "typechange", "submodule")
_OPERATION_FILES = (
    ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"), ("rebase-merge", "rebase"), ("rebase-apply", "rebase"),
)


class GitReviewError(Exception):
    """Validation or git failure; ``code`` is stable, ``message`` is user facing."""

    def __init__(self, code: str, message: str, path: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path  # offending file (repo-relative) when applicable


@dataclass(frozen=True)
class Repo:
    id: str                 # posix path relative to the project root; "." = root (or enclosing) repo
    path: Path              # resolved absolute top level of the repository
    kind: str               # "root" | "outer" | "nested" | "submodule"
    scope: str = ""         # outer repos only: project root relative to the top level (pathspec limit)
    parent: str = ""        # id of the closest enclosing discovered repo ("" for the top one)
    excluded: tuple[str, ...] = ()  # repo-relative posix paths of repos nested inside this one


@dataclass(frozen=True)
class FileChange:
    repo: str               # Repo.id that owns the file
    path: str               # posix path relative to the repo top level
    kind: str               # one of KINDS
    index: str = "."        # X of porcelain v2 XY ("?" for untracked)
    worktree: str = "."     # Y of porcelain v2 XY
    orig_path: str = ""     # rename/copy source (repo-relative)
    submodule: str = ""     # porcelain v2 <sub> field when it is not "N..." (e.g. "SC..")

    @property
    def staged(self) -> bool:
        return self.index not in (".", "?")

    @property
    def unstaged(self) -> bool:
        return self.worktree != "."

    @property
    def partially_staged(self) -> bool:
        return self.staged and self.worktree not in (".", "?")

    @property
    def conflicted(self) -> bool:
        return self.kind == "conflicted"

    @property
    def committable(self) -> bool:
        if self.conflicted:
            return False
        if self.kind == "submodule":
            return len(self.submodule) > 1 and self.submodule[1] == "C"  # pointer moved
        return True


@dataclass
class RepoStatus:
    branch: str             # "" when detached
    head: str               # full hash, "" for an unborn branch
    files: list[FileChange] = field(default_factory=list)
    truncated: bool = False  # more than MAX_FILES_PER_REPO entries
    hidden: list[str] = field(default_factory=list)  # untracked dirs that are repos not discovered
    operation: str = ""     # "merge" | "cherry-pick" | "revert" | "rebase" | ""


@dataclass(frozen=True)
class FileDiff:
    text: str
    binary: bool
    truncated: bool
    size: int               # bytes of the full diff before clipping
    staged_text: str = ""   # only for partially staged files
    unstaged_text: str = ""


@dataclass(frozen=True)
class CommitResult:
    hash: str
    files: list[str]        # repo-relative paths recorded in the new commit
    preserved_staged: list[str]  # other staged paths left untouched in the index


async def run_git(cwd: Path, *args: str, stdin: bytes | None = None,
                  timeout: float = GIT_TIMEOUT, ok_codes: tuple[int, ...] = (0,)) -> bytes:
    """Run ``git -C cwd -c core.quotepath=off <args>``; stdout bytes or GitReviewError."""
    env = _git_env()
    env["GIT_LITERAL_PATHSPECS"] = "1"  # paths are never globs
    env["LC_ALL"] = "C"                 # stable messages
    env["GIT_OPTIONAL_LOCKS"] = "0"     # status never touches the index
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", str(cwd), "-c", "core.quotepath=off", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise GitReviewError("git_missing", "git is not installed") from exc
    except OSError as exc:
        raise GitReviewError("git_failed", str(exc)) from exc
    try:
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout)
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        name = args[0] if args else "git"
        raise GitReviewError("timeout", f"git {name} timed out after {int(timeout)} s") from exc
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if (process.returncode or 0) not in ok_codes:
        text = _tail(err.decode("utf-8", "replace"))
        raise GitReviewError("git_failed", text or f"git {args[0] if args else ''} failed")
    return out


# --- discovery ---------------------------------------------------------------

def _toplevel(path: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _scan(root: Path) -> list[Path]:
    """Directories at or below ``root`` that contain a ``.git`` entry (breadth bounded)."""
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and len(found) < MAX_REPOS:
        current, depth = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            found.append(current)
        if depth >= MAX_DEPTH:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return found


def discover(root: Path) -> list[Repo]:
    """Git repositories at or below ``root`` (plus the one enclosing it), outermost first."""
    root = root.resolve()
    entries: list[tuple[str, Path, str, str]] = []  # (id, path, kind, scope)
    if (root / ".git").exists():
        entries.append((".", root, "root", ""))
    else:
        top = _toplevel(root)
        if top is not None and (top == root or top in root.parents):
            entries.append((".", top, "outer", root.relative_to(top).as_posix()))
    for directory in _scan(root):
        if directory == root:
            continue
        kind = "submodule" if (directory / ".git").is_file() else "nested"
        entries.append((directory.relative_to(root).as_posix(), directory, kind, ""))
    # parent: deepest discovered repo whose path strictly encloses the directory
    paths_by_id = {e[0]: e[1] for e in entries}
    parents: dict[str, str] = {}
    for rid, path, _, _ in entries:
        best = ""
        for other_id, other in paths_by_id.items():
            if other_id == rid or other == path or other not in path.parents:
                continue
            if not best or len(paths_by_id[best].parts) < len(other.parts):
                best = other_id
        parents[rid] = best
    repos: list[Repo] = []
    for rid, path, kind, scope in entries:
        excluded: list[str] = []
        for cid, cpath, _, _ in entries:
            if parents.get(cid) == rid and cid != rid:
                excluded.append(cpath.relative_to(path).as_posix())
        repos.append(Repo(rid, path, kind, scope, parents[rid], tuple(sorted(excluded))))
    repos.sort(key=lambda r: (r.id != ".", r.id.casefold()))
    return repos


# --- status ------------------------------------------------------------------

def _kind_of(xy: str, sub: str, is_rename: bool, score: str) -> str:
    if sub and not sub.startswith("N"):
        return "submodule"
    if is_rename:
        return "copied" if score.startswith("C") else "renamed"
    if "T" in xy:
        return "typechange"
    if "D" in xy:
        return "deleted"
    if "A" in xy:  # also intent-to-add (".A")
        return "added"
    return "modified"


def parse_status_v2(raw: bytes, repo: Repo) -> RepoStatus:
    """Parse ``git status --porcelain=v2 -z --branch`` output limited to the repo's own files."""
    tokens = raw.decode("utf-8", "surrogateescape").split("\0")
    status = RepoStatus(branch="", head="")
    files: list[FileChange] = []
    i = 0
    while i < len(tokens):
        rec = tokens[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# branch.oid "):
            value = rec[len("# branch.oid "):]
            status.head = "" if value == "(initial)" else value
            continue
        if rec.startswith("# branch.head "):
            value = rec[len("# branch.head "):]
            status.branch = "" if value == "(detached)" else value
            continue
        if rec.startswith("# "):
            continue
        tag = rec[0]
        if tag == "1":
            f = rec.split(" ", 8)
            if len(f) < 9:
                continue
            xy, sub, path = f[1], f[2], f[8]
            change = FileChange(repo.id, path, _kind_of(xy, sub, False, ""), xy[0], xy[1],
                                submodule="" if sub.startswith("N") else sub)
        elif tag == "2":
            f = rec.split(" ", 9)
            orig = tokens[i] if i < len(tokens) else ""
            i += 1
            if len(f) < 10:
                continue
            xy, sub, score, path = f[1], f[2], f[8], f[9]
            change = FileChange(repo.id, path, _kind_of(xy, sub, True, score), xy[0], xy[1],
                                orig_path=orig, submodule="" if sub.startswith("N") else sub)
        elif tag == "u":
            f = rec.split(" ", 10)
            if len(f) < 11:
                continue
            change = FileChange(repo.id, f[10], "conflicted", f[1][0], f[1][1])
        elif tag == "?":
            path = rec[2:]
            if path.endswith("/"):
                status.hidden.append(path.rstrip("/"))  # nested repo that was not discovered
                continue
            change = FileChange(repo.id, path, "untracked", "?", "?")
        else:
            continue  # "!" ignored and unknown records
        if _outside_limits(repo, change):
            continue
        files.append(change)
    if len(files) > MAX_FILES_PER_REPO:
        status.truncated = True
        files = files[:MAX_FILES_PER_REPO]
    files.sort(key=lambda c: c.path.casefold())
    status.files = files
    if repo.scope:
        status.hidden = [h for h in status.hidden if h == repo.scope or h.startswith(repo.scope + "/")]
    status.hidden = [h for h in status.hidden if not _in_excluded(repo, h)]
    return status


def _in_excluded(repo: Repo, path: str) -> bool:
    return any(path == ex or path.startswith(ex + "/") for ex in repo.excluded)


def _outside_limits(repo: Repo, change: FileChange) -> bool:
    path = change.path
    if repo.scope and not (path == repo.scope or path.startswith(repo.scope + "/")):
        return True
    for ex in repo.excluded:
        if path.rstrip("/") == ex or path.startswith(ex + "/"):
            if change.kind == "submodule" and path == ex:
                return False  # submodule pointer recorded in the parent
            return True
    return False


async def status(repo: Repo) -> RepoStatus:
    """Current pending changes of one repository (own files only)."""
    raw = await run_git(repo.path, "status", "--porcelain=v2", "-z", "--branch",
                        "--untracked-files=all")
    result = parse_status_v2(raw, repo)
    names = [name for name, _ in _OPERATION_FILES]
    args: list[str] = ["rev-parse"]
    for name in names:
        args += ["--git-path", name]
    try:
        out = (await run_git(repo.path, *args)).decode("utf-8", "replace").splitlines()
    except GitReviewError:
        out = []
    for (name, operation), rel in zip(_OPERATION_FILES, out):
        target = Path(rel)
        if not target.is_absolute():
            target = repo.path / target
        if target.exists():
            result.operation = operation
            break
    return result


# --- paths and fingerprints ---------------------------------------------------

def project_path(repo: Repo, path: str) -> str:
    """Path relative to the project root."""
    if repo.kind == "outer" and repo.scope:
        return path[len(repo.scope) + 1:] if path.startswith(repo.scope + "/") else path
    if repo.id == ".":
        return path
    return f"{repo.id}/{path}"


def file_key(repo_id: str, path: str) -> str:
    """Opaque stable id of a file for clients."""
    return hashlib.sha1(f"{repo_id}\0{path}".encode()).hexdigest()[:16]


def fingerprint(repo: Repo, change: FileChange) -> str:
    """Hash of the change state and the working file stat; detects edits after review."""
    try:
        info = os.stat(repo.path / change.path)
        stat = f"{info.st_size}|{info.st_mtime_ns}"
    except OSError:
        stat = "missing"
    raw = f"{change.index}{change.worktree}|{change.orig_path}|{stat}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def validate_rel_path(path: str) -> str:
    """Reject unsafe client paths (``invalid_path``); returns the normalized posix path."""
    if not isinstance(path, str) or not path or "\0" in path:
        raise GitReviewError("invalid_path", "invalid path")
    if path.startswith(("/", "\\", "-")) or (len(path) > 1 and path[1] == ":"):
        raise GitReviewError("invalid_path", "invalid path", path)
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise GitReviewError("invalid_path", "invalid path", path)
    return "/".join(parts)


# --- diff --------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _clip(raw: bytes, limit: int) -> tuple[str, bool]:
    """Decode at most ``limit`` bytes of ``raw``; the flag tells whether it was clipped."""
    return _decode(raw[:limit]), len(raw) > limit


async def diff(repo: Repo, change: FileChange, has_head: bool,
               max_bytes: int = DIFF_DEFAULT_BYTES, split: bool = True) -> FileDiff:
    """Unified diff of one pending file against HEAD (or index + worktree without HEAD).

    For partially staged files ``split`` also returns the staged and unstaged
    parts; callers that only need the combined text pass ``split=False`` to
    skip the two extra git runs. Every part is clipped to ``max_bytes``.
    """
    base = ["diff", "--no-color", "--no-ext-diff", "-M"]
# a copy leaves its source untouched: only a rename diffs against the source path
    paths_ = ([change.orig_path, change.path]
              if change.orig_path and change.kind == "renamed" else [change.path])
    staged_raw = unstaged_raw = b""
    if change.kind == "untracked":
        raw = await run_git(repo.path, *base, "--no-index", "--", "/dev/null", change.path,
                            ok_codes=(0, 1))
    elif change.kind == "submodule":
        raw = await run_git(repo.path, *base, "--submodule=short", "HEAD", "--", change.path)
    elif has_head:
        raw = await run_git(repo.path, *base, "HEAD", "--", *paths_)
    else:
        raw = (await run_git(repo.path, *base, "--cached", "--", *paths_)
               + await run_git(repo.path, *base, "--", *paths_))
    if split and change.partially_staged:
        staged_raw = await run_git(repo.path, *base, "--cached", "--", *paths_)
        unstaged_raw = await run_git(repo.path, *base, "--", *paths_)
    limit = max(1024, min(int(max_bytes), DIFF_MAX_BYTES))
    size = len(raw)
    text, truncated = _clip(raw, limit)
    staged_text, staged_cut = _clip(staged_raw, limit)
    unstaged_text, unstaged_cut = _clip(unstaged_raw, limit)
    truncated = truncated or staged_cut or unstaged_cut
    binary = any(
        line.startswith("Binary files ") or "GIT binary patch" in line
        for line in text.splitlines()
    )
    return FileDiff(text, binary, truncated, size, staged_text, unstaged_text)


# --- commit ------------------------------------------------------------------

_LOCKS: dict[str, asyncio.Lock] = {}


def repo_lock(path: Path | str) -> asyncio.Lock:
    """Per-repository lock that serializes commits."""
    key = str(path)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


def normalize_message(message: str) -> str:
    """Strip trailing spaces and surrounding blank lines; ``empty_message`` when nothing is left."""
    lines = [line.rstrip() for line in (message or "").replace("\r\n", "\n").split("\n")]
    text = "\n".join(lines).strip("\n")
    if not text.strip():
        raise GitReviewError("empty_message", "commit message is required")
    return text


def select_changes(repo: Repo, st: RepoStatus, paths: list[str], *,
                   include_unstaged: bool = False,
                   expected: dict[str, str] | None = None) -> list[FileChange]:
    """Validate that every requested path is a pending, committable file of ``repo``."""
    by_path = {c.path: c for c in st.files}
    selected: list[FileChange] = []
    seen: set[str] = set()
    for raw in paths:
        path = validate_rel_path(raw)
        if path in seen:
            continue
        seen.add(path)
        change = by_path.get(path)
        if change is None:
            raise GitReviewError("not_pending", "file is not pending", path)
        if change.conflicted:
            raise GitReviewError("conflicted", "file has merge conflicts", path)
        if not change.committable:
            raise GitReviewError("not_committable", "change cannot be committed", path)
        if change.partially_staged and not include_unstaged:
            raise GitReviewError(
                "partially_staged",
                "file has staged and unstaged changes; the commit would take the full "
                "working tree content",
                path,
            )
        if expected:
            want = expected.get(path) or expected.get(raw)
            if want and want != fingerprint(repo, change):
                raise GitReviewError("stale", "file changed since it was reviewed", path)
        selected.append(change)
    return selected


async def _staged_paths(repo: Repo, has_head: bool) -> set[str]:
    if has_head:
        raw = await run_git(repo.path, "diff", "--cached", "--name-only", "-z")
    else:
        raw = await run_git(repo.path, "ls-files", "-z")
    return {p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p}


async def commit(repo: Repo, paths: list[str], message: str, *,
                 include_unstaged: bool = False,
                 expected: dict[str, str] | None = None) -> CommitResult:
    """Commit exactly ``paths`` of ``repo`` (``git commit --only``); other staged paths are kept."""
    msg = normalize_message(message)
    async with repo_lock(repo.path):
        st = await status(repo)
        if st.operation:
            raise GitReviewError("operation_in_progress",
                                 f"a git {st.operation} is in progress")
        sel = select_changes(repo, st, paths, include_unstaged=include_unstaged,
                             expected=expected)
        if not sel:
            raise GitReviewError("not_pending", "no files to commit")
        has_head = bool(st.head)
        # only a rename removes its source; a copy leaves the source in place, so the
        # source was not requested and must keep its own (possibly staged) state
        pathspecs = sorted({c.path for c in sel}
                           | {c.orig_path for c in sel if c.orig_path and c.kind == "renamed"})
        staged_before = await _staged_paths(repo, has_head)
        untracked = [c.path for c in sel if c.kind == "untracked"]
        if untracked:
            await run_git(repo.path, "add", "--", *untracked)
        try:
            await run_git(repo.path, "commit", "--only", "-F", "-", "--", *pathspecs,
                          stdin=msg.encode("utf-8"), timeout=COMMIT_TIMEOUT)
        except GitReviewError as exc:
            if untracked:
                with contextlib.suppress(GitReviewError):
                    if has_head:
                        await run_git(repo.path, "reset", "-q", "--", *untracked)
                    else:
                        await run_git(repo.path, "rm", "--cached", "-q", "--", *untracked)
            if exc.code == "timeout":
                raise
            raise GitReviewError("commit_failed", exc.message) from exc
        head = (await run_git(repo.path, "rev-parse", "HEAD")).decode().strip()
        raw = await run_git(repo.path, "diff-tree", "--root", "--no-commit-id", "--name-only",
                            "-r", "-z", "HEAD")
        files = [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]
        still = await _staged_paths(repo, True)
        preserved = sorted((staged_before - set(pathspecs)) & still)
    return CommitResult(head, files, preserved)
