"""Branch operations (push, pull, switch, abort) and pending-change summaries of project repositories.

Repositories come from ``gitrepos.discover_all`` (nested at any depth). Local
operations reuse ``gitrepos.run_git``; push and pull reuse the detached,
tree-killing runner of ``projectsetup`` because they may hang on the network.
Every mutation holds ``gitrepos.repo_lock`` so it never races a review commit.
Failures raise ``GitReviewError`` with a stable ``code``; nothing is forced:
no ``--force``, no ``reset``, no stash, no branch creation.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import gitrepos
from .gitrepos import GitReviewError, Repo, RepoStatus
from .projectsetup import TIMED_OUT, _git_env, _run_process, redact_url

__all__ = [
    "OpResult", "Branch", "PendingSummary", "PULL_MODES", "classify_failure", "redact_text",
    "validate_branch", "list_branches", "checkout", "push", "pull", "abort",
    "pending_summary", "pending_summaries", "invalidate",
]

NETWORK_TIMEOUT = 300.0      # seconds for push / pull
LOCAL_TIMEOUT = 120.0        # seconds for switch / abort
SUMMARY_TTL = 15.0           # seconds a pending summary is reused
SUMMARY_CONCURRENCY = 4      # projects scanned at the same time
STATUS_CONCURRENCY = 8       # git status processes per project at the same time
PULL_MODES = ("ff-only", "merge", "rebase")
REMOTE_PROTOCOLS = "file:git:http:https:ssh"  # remotes are configured by the repo owner, not by clients
_ABORT = {
    "merge": ("merge", "--abort"), "rebase": ("rebase", "--abort"),
    "cherry-pick": ("cherry-pick", "--abort"), "revert": ("revert", "--abort"),
}
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


# --- low level helpers --------------------------------------------------------

def redact_text(text: str) -> str:
    """Mask credentials of every URL inside ``text`` (git output may echo remote URLs)."""
    return _URL.sub(lambda m: redact_url(m.group(0)), text or "")


def _network_env() -> dict[str, str]:
    env = _git_env()
    env["GIT_ALLOW_PROTOCOL"] = REMOTE_PROTOCOLS  # _git_env() blocks local/file remotes
    env["LC_ALL"] = "C"                 # stable messages for classify_failure
    env["GIT_EDITOR"] = "true"          # never wait on an editor (no tty)
    env["GIT_MERGE_AUTOEDIT"] = "no"
    return env


async def _run_network(repo: Repo, *args: str, timeout: float = NETWORK_TIMEOUT) -> tuple[int, str]:
    """Run a networked git command; ``(returncode, redacted output tail)``."""
    executable = shutil.which("git")
    if executable is None:
        raise GitReviewError("git_missing", "git is not installed")
    code, output = await _run_process(
        [executable, "-C", str(repo.path), "-c", "core.quotepath=off", *args],
        repo.path, timeout, _network_env(),
    )
    if code == TIMED_OUT:
        raise GitReviewError("timeout", f"git {args[0]} timed out after {int(timeout)} s")
    return code, redact_text(output)


_FAILURES = (  # first match wins: order matters
    ("local_changes", ("would be overwritten by", "please commit your changes or stash them",
                       "cannot pull with rebase", "you have unstaged changes",
                       "your index contains uncommitted changes")),
    ("diverged", ("not possible to fast-forward", "diverging branches",
                  "need to specify how to reconcile divergent branches")),
    ("push_rejected", ("[rejected]", "[remote rejected]", "non-fast-forward", "fetch first",
                       "updates were rejected")),
    # before remote_unreachable: git prints "Permission denied" next to "Could not read from remote repository"
    ("auth_failed", ("authentication failed", "permission denied", "could not read username",
                     "could not read password", "terminal prompts disabled",
                     "host key verification failed", "invalid username or password")),
    ("no_remote", ("does not appear to be a git repository", "no such remote",
                   "no configured push destination")),
    ("remote_unreachable", ("could not resolve host", "connection refused", "connection timed out",
                            "network is unreachable", "unable to access",
                            "could not read from remote repository", "operation timed out")),
)


def classify_failure(output: str, action: str) -> GitReviewError:
    """Map git output of a failed ``action`` to a ``GitReviewError`` with a stable code."""
    text = redact_text(output).strip()
    lowered = text.casefold()
    message = f"git {action} failed: {text}" if text else f"git {action} failed"
    for code, needles in _FAILURES:
        if any(n in lowered for n in needles):
            return GitReviewError(code, message)
    return GitReviewError("git_failed", message)


# --- results -------------------------------------------------------------------

@dataclass
class OpResult:
    action: str                 # push | pull | checkout | abort
    outcome: str                # push: pushed/created/up_to_date; pull: updated/up_to_date/conflict;
                                # checkout: switched/unchanged; abort: aborted
    branch: str = ""            # branch after the operation ("" when detached)
    previous_branch: str = ""   # checkout only
    remote: str = ""            # push only
    upstream: str = ""
    head_before: str = ""
    head_after: str = ""
    commits: int = 0            # pull: commits brought in
    conflicts: list[str] = field(default_factory=list)  # repo-relative conflicted paths
    in_progress: str = ""       # git operation left in progress (merge/rebase) or aborted
    output: str = ""            # redacted tail of the git output

    def to_dict(self) -> dict:
        return asdict(self)


async def _checked_status(repo: Repo) -> RepoStatus:
    """Status of ``repo``; rejects detached HEAD, operations in progress and conflicts."""
    st = await gitrepos.status(repo)
    if st.operation:
        raise GitReviewError("operation_in_progress", f"a git {st.operation} is in progress")
    conflicted = [c.path for c in st.files if c.conflicted]
    if conflicted:
        raise GitReviewError("conflicted", "repository has merge conflicts", conflicted[0])
    if not st.branch:
        raise GitReviewError("detached", "HEAD is detached: no current branch")
    return st


async def _config(repo: Repo, key: str) -> str:
    out = await gitrepos.run_git(repo.path, "config", "--get", key, ok_codes=(0, 1))
    return out.decode("utf-8", "replace").strip()


async def _remotes(repo: Repo) -> list[str]:
    out = await gitrepos.run_git(repo.path, "remote")
    return [r for r in out.decode("utf-8", "replace").split() if r]


# --- branches ------------------------------------------------------------------

@dataclass(frozen=True)
class Branch:
    name: str
    head: str
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    gone: bool = False
    current: bool = False
    date: str = ""      # committer date, ISO 8601
    subject: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


_BRANCH_FORMAT = ("%(refname)%00%(objectname)%00%(upstream:short)%00"
                  "%(upstream:track,nobracket)%00%(HEAD)%00%(committerdate:iso-strict)%00"
                  "%(contents:subject)")
_AHEAD = re.compile(r"ahead (\d+)")
_BEHIND = re.compile(r"behind (\d+)")


async def list_branches(repo: Repo) -> list[Branch]:
    """Local branches (``refs/heads``) with upstream tracking; current branch first."""
    raw = await gitrepos.run_git(repo.path, "for-each-ref", f"--format={_BRANCH_FORMAT}", "refs/heads")
    items: list[Branch] = []
    for line in raw.decode("utf-8", "replace").split("\n"):
        fields = line.split("\0")
        if len(fields) < 7:
            continue
        refname, oid, upstream, track, marker, date, subject = fields[:7]
        gone = track.strip() == "gone"
        ahead = _AHEAD.search(track)
        behind = _BEHIND.search(track)
        items.append(Branch(
            name=refname[len("refs/heads/"):], head=oid, upstream=upstream,
            ahead=int(ahead.group(1)) if ahead else 0,
            behind=int(behind.group(1)) if behind else 0,
            gone=gone, current=marker.strip() == "*", date=date, subject=subject,
        ))
    return sorted(items, key=lambda b: (not b.current, b.name.casefold()))


def validate_branch(name: object) -> str:
    """Reject empty, option-like or multi-line names (``invalid_branch``)."""
    if not isinstance(name, str) or not name.strip():
        raise GitReviewError("invalid_branch", "branch is required")
    name = name.strip()
    if name.startswith("-") or any(ch in name for ch in ("\0", "\n", "\r")):
        raise GitReviewError("invalid_branch", f"invalid branch name: {name}")
    return name


async def checkout(repo: Repo, branch: str) -> OpResult:
    """Switch to an EXISTING local branch (``git switch --no-guess``); never creates branches."""
    name = validate_branch(branch)
    async with gitrepos.repo_lock(repo.path):
        st = await gitrepos.status(repo)  # a detached HEAD is allowed: it is a way out of it
        if st.operation:
            raise GitReviewError("operation_in_progress", f"a git {st.operation} is in progress")
        conflicted = [c.path for c in st.files if c.conflicted]
        if conflicted:
            raise GitReviewError("conflicted", "repository has merge conflicts", conflicted[0])
        if name not in {b.name for b in await list_branches(repo)}:
            raise GitReviewError("branch_not_found", f"local branch not found: {name}")
        if st.branch == name:
            return OpResult("checkout", "unchanged", branch=name, previous_branch=name,
                            upstream=st.upstream, head_before=st.head, head_after=st.head)
        try:
            await gitrepos.run_git(repo.path, "switch", "--no-guess", name, timeout=LOCAL_TIMEOUT)
        except GitReviewError as exc:
            if exc.code in ("timeout", "git_missing"):
                raise
            raise classify_failure(exc.message, "switch") from exc
        after = await gitrepos.status(repo)
        return OpResult("checkout", "switched", branch=after.branch, previous_branch=st.branch,
                        upstream=after.upstream, head_before=st.head, head_after=after.head)


# --- push / pull / abort ---------------------------------------------------------

async def push(repo: Repo, *, set_upstream: bool = False, remote: str = "") -> OpResult:
    """Push the current branch to its upstream (or publish it with ``set_upstream``)."""
    async with gitrepos.repo_lock(repo.path):
        st = await _checked_status(repo)
        if not st.head:
            raise GitReviewError("no_commits", "the branch has no commits yet")
        b = st.branch
        remotes = await _remotes(repo)
        tracked = ""     # remote name when the upstream is the same-named branch (safe plain push)
        mismatch = False
        merge = ""
        if st.upstream and not st.upstream_gone:
            tracked = await _config(repo, f"branch.{b}.remote")
            if not tracked:
                raise GitReviewError("no_remote", f"branch {b} has no configured remote")
            merge = await _config(repo, f"branch.{b}.merge") or f"refs/heads/{b}"
            # plain ``git push`` refuses a differently named upstream (push.default=simple);
            # pushing there (git switch -c x origin/main) would update someone else's branch
            mismatch = merge != f"refs/heads/{b}" or tracked == "."
        if tracked and not mismatch:
            remote_name = tracked
            args = ["push", "--porcelain", remote_name, f"refs/heads/{b}:{merge}"]
        else:
            if not set_upstream:
                if mismatch:
                    raise GitReviewError(
                        "upstream_mismatch",
                        f"upstream {st.upstream} does not match branch {b}; "
                        f"push it with set_upstream to publish {b}")
                if st.upstream_gone:
                    raise GitReviewError(
                        "upstream_gone",
                        f"upstream {st.upstream} no longer exists; retry with set_upstream to publish {b}")
                raise GitReviewError(
                    "no_upstream", f"branch {b} has no upstream; retry with set_upstream to publish it")
            if remote:
                if remote not in remotes:
                    raise GitReviewError("no_remote", f"remote not found: {remote}")
                remote_name = remote
            elif len(remotes) == 1:
                remote_name = remotes[0]
            elif "origin" in remotes:
                remote_name = "origin"
            elif not remotes:
                raise GitReviewError("no_remote", "the repository has no remotes")
            else:
                raise GitReviewError(
                    "ambiguous_remote", "several remotes: " + ", ".join(remotes) + "; pass remote")
            args = ["push", "--porcelain", "--set-upstream", remote_name, f"refs/heads/{b}:refs/heads/{b}"]
        code, out = await _run_network(repo, *args)
        # porcelain lines: <flag>\t<from>:<to>\t<summary>; some versions exit 0 even when a ref is rejected
        flags = [line.split("\t", 1)[0] for line in out.splitlines()
                 if "\t" in line and len(line.split("\t", 1)[0]) == 1]
        if code != 0 or "!" in flags:
            raise classify_failure(out, "push")
        if flags and all(f == "=" for f in flags):
            outcome = "up_to_date"
        else:
            outcome = "created" if "*" in flags else "pushed"
        after = await gitrepos.status(repo)
        return OpResult("push", outcome, branch=b, remote=remote_name, upstream=after.upstream,
                        head_before=st.head, head_after=after.head, output=out)


_PULL_FLAGS = {
    "ff-only": ("--no-rebase", "--ff-only"),
    "merge": ("--no-rebase", "--no-edit"),
    "rebase": ("--rebase",),
}


async def pull(repo: Repo, *, mode: str = "ff-only") -> OpResult:
    """Pull the upstream of the current branch; conflicts are a result, not an error."""
    if mode not in PULL_MODES:
        raise GitReviewError("invalid_mode", "mode must be one of: " + ", ".join(PULL_MODES))
    async with gitrepos.repo_lock(repo.path):
        st = await _checked_status(repo)
        if not st.upstream:
            raise GitReviewError("no_upstream", f"branch {st.branch} has no upstream")
        if st.upstream_gone:
            raise GitReviewError("upstream_gone", f"upstream {st.upstream} no longer exists")
        code, out = await _run_network(repo, "pull", *_PULL_FLAGS[mode])
        after = await gitrepos.status(repo)
        if code != 0:
            conflicts = [c.path for c in after.files if c.conflicted]
            if after.operation or conflicts:
                # during a rebase HEAD is detached, hence the fallback to the branch before the pull
                return OpResult("pull", "conflict", branch=after.branch or st.branch,
                                upstream=st.upstream, head_before=st.head, head_after=after.head,
                                conflicts=conflicts, in_progress=after.operation, output=out)
            raise classify_failure(out, "pull")
        commits = 0
        if after.head and st.head and after.head != st.head:
            try:
                counted = await gitrepos.run_git(repo.path, "rev-list", "--count", f"{st.head}..{after.head}")
                commits = int(counted.decode().strip() or 0)
            except (GitReviewError, ValueError):
                commits = 0
        return OpResult("pull", "up_to_date" if after.head == st.head else "updated",
                        branch=after.branch, upstream=after.upstream, head_before=st.head,
                        head_after=after.head, commits=commits, output=out)


async def abort(repo: Repo) -> OpResult:
    """Abort the merge, rebase, cherry-pick or revert in progress (``no_operation`` otherwise)."""
    async with gitrepos.repo_lock(repo.path):
        st = await gitrepos.status(repo)
        if not st.operation:
            raise GitReviewError("no_operation", "no git operation in progress")
        command = _ABORT.get(st.operation)
        if command is None:
            raise GitReviewError("no_operation", f"cannot abort a {st.operation} operation")
        try:
            await gitrepos.run_git(repo.path, *command, timeout=LOCAL_TIMEOUT)
        except GitReviewError as exc:
            if exc.code == "timeout":
                raise
            raise GitReviewError("git_failed", redact_text(exc.message)) from exc
        after = await gitrepos.status(repo)
        return OpResult("abort", "aborted", branch=after.branch, head_before=st.head,
                        head_after=after.head, in_progress=st.operation)


# --- pending summary ---------------------------------------------------------------

@dataclass
class PendingSummary:
    git: bool = False                 # at least one repository found
    dirty: bool = False               # files > 0 in any repository
    repos: int = 0
    dirty_repos: list[str] = field(default_factory=list)   # repo ids with pending files
    files: int = 0                    # pending files (lower bound when truncated)
    conflicts: int = 0
    ahead: int = 0                    # sum of unpushed commits
    behind: int = 0                   # sum of commits to pull
    operations: dict[str, str] = field(default_factory=dict)  # repo id -> merge/rebase/...
    errors: dict[str, str] = field(default_factory=dict)      # repo id -> message
    hidden: int = 0                   # nested repos not scanned (ignored folders)
    truncated: bool = False           # some repository exceeded MAX_FILES_PER_REPO
    incomplete: bool = False          # discovery budget reached (MAX_REPOS / MAX_SCAN_DIRS)
    checked_at: str = ""              # ISO 8601 with offset

    def to_dict(self) -> dict:
        return asdict(self)


_CACHE: dict[str, tuple[float, PendingSummary]] = {}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def invalidate(root: Path | str) -> None:
    """Forget the cached summary of a project (after a commit, push, pull, checkout...)."""
    _CACHE.pop(str(Path(root).resolve()), None)


async def _compute(root: Path) -> PendingSummary:
    summary = PendingSummary(checked_at=_now())
    if not gitrepos.git_available():
        return summary
    repos, complete = await asyncio.to_thread(gitrepos.discover_all, root)
    sem = asyncio.Semaphore(STATUS_CONCURRENCY)  # hundreds of repos must not exhaust file descriptors

    async def one(repo: Repo) -> RepoStatus:
        async with sem:
            return await gitrepos.status(repo)

    results = await asyncio.gather(*(one(r) for r in repos), return_exceptions=True)
    for repo, res in zip(repos, results):
        if isinstance(res, GitReviewError):
            summary.errors[repo.id] = res.message
            continue
        if isinstance(res, Exception):  # a broken repo must not take the project list down
            summary.errors[repo.id] = str(res) or type(res).__name__
            continue
        if isinstance(res, BaseException):
            raise res
        summary.files += len(res.files)
        summary.conflicts += sum(1 for c in res.files if c.conflicted)
        summary.ahead += res.ahead
        summary.behind += res.behind
        summary.hidden += len(res.hidden)
        summary.truncated = summary.truncated or res.truncated
        if res.operation:
            summary.operations[repo.id] = res.operation
        if res.files:
            summary.dirty_repos.append(repo.id)
    summary.repos = len(repos)
    summary.git = bool(repos)
    summary.dirty = summary.files > 0
    summary.incomplete = not complete
    return summary


async def pending_summary(root: Path, *, refresh: bool = False) -> PendingSummary:
    """Pending changes of every repository at or below ``root`` (cached ``SUMMARY_TTL`` s)."""
    key = str(Path(root).resolve())
    cached = _CACHE.get(key)
    if not refresh and cached and time.monotonic() - cached[0] < SUMMARY_TTL:
        return cached[1]
    summary = await _compute(Path(key))
    _CACHE[key] = (time.monotonic(), summary)
    return summary


async def pending_summaries(roots: list[Path], *, refresh: bool = False) -> list[PendingSummary]:
    """``pending_summary`` of several projects, at most ``SUMMARY_CONCURRENCY`` at a time."""
    sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)

    async def one(root: Path) -> PendingSummary:
        async with sem:
            return await pending_summary(root, refresh=refresh)

    return list(await asyncio.gather(*(one(r) for r in roots)))
