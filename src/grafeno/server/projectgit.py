"""Per-project git operations shared by the REST and WebSocket APIs: pending summary, branches, push, pull, checkout and abort."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .. import gitrepos, gitsync, remotesession
from .actions import ApiError, _coerce_bool, list_projects
from .projectdocs import is_remote_only, resolve_project, task_dirs
from .reviews import _STATUS as _REVIEW_STATUS
from .reviews import _guard, _text, repo_payload

if TYPE_CHECKING:
    from .service import ServerService

_STATUS = {
    **_REVIEW_STATUS,
    "invalid_branch": 400, "invalid_mode": 400, "branch_not_found": 404,
    "detached": 409, "no_commits": 409, "no_upstream": 409, "upstream_gone": 409,
    "no_remote": 409, "ambiguous_remote": 409, "local_changes": 409, "diverged": 409,
    "push_rejected": 409, "no_operation": 409, "upstream_mismatch": 409,
    "auth_failed": 502, "remote_unreachable": 502,
}


def _git_error(exc: gitrepos.GitReviewError) -> ApiError:
    message = f"{exc.message}: {exc.path}" if exc.path else exc.message
    return ApiError(_STATUS.get(exc.code, 400), message, code=exc.code)


def _log(service: "ServerService", text: str) -> None:
    """Activity log line; never carries git output (it may echo remote URLs)."""
    log = getattr(service, "_log", None)
    if log:
        log(text)


async def _resolve_repo(service: "ServerService", params: dict) -> tuple[dict, Path, gitrepos.Repo]:
    _guard()  # 409 code=remote in remote session mode
    item, root = resolve_project(service, _text(params, "workdir"), _text(params, "project"))
    if not gitrepos.git_available():
        raise ApiError(503, "git is not installed", code="git_missing")
    repo_id = _text(params, "repo") or "."
    repos = await asyncio.to_thread(gitrepos.discover, root)
    repo = next((r for r in repos if r.id == repo_id), None)
    if repo is None:
        raise ApiError(409, f"repository not found: {repo_id}", code="not_a_repo")
    return item, root, repo


async def _op_body(item: dict, root: Path, repo: gitrepos.Repo, result: gitsync.OpResult) -> dict:
    gitsync.invalidate(root)
    try:
        st, err = await gitrepos.status(repo), ""
    except gitrepos.GitReviewError as exc:
        st, err = None, exc.message
    pending = await gitsync.pending_summary(root, refresh=True)
    return {
        "project": item,
        "repo": repo_payload(repo, st, err),
        "result": result.to_dict(),
        "pending": pending.to_dict(),
    }


async def _run_op(service: "ServerService", name: str, payload: dict, call) -> dict:
    """Resolve the repo, run ``call(repo)`` and build the common response body."""
    item, root, repo = await _resolve_repo(service, payload)
    try:
        result = await call(repo)
    except gitrepos.GitReviewError as exc:
        gitsync.invalidate(root)
        _log(service, f"git {name} {root} [{repo.id}]: failed ({exc.code})")
        raise _git_error(exc) from exc
    _log(service, f"git {name} {root} [{repo.id}]: {result.outcome}")
    return await _op_body(item, root, repo, result)


async def list_changes(service: "ServerService", workdir: str = "", project: str = "",
                       refresh: bool = False) -> dict:
    """Pending-change summary per project (all catalogue projects when no filter is given)."""
    available = gitrepos.git_available()
    if remotesession.active():
        return {"projects": [], "session": True, "git_available": available}
    if workdir.strip() or project.strip():
        item, _ = resolve_project(service, workdir, project)
        items = [item]
    else:
        items = list_projects(service)["projects"]
    local_dirs, remote_dirs = task_dirs()
    targets: list[tuple[dict, Path | None, bool, bool]] = []  # item, root, remote, exists
    for item in items:
        remote = is_remote_only(item, local_dirs, remote_dirs)
        root = None
        if not remote:  # never mount sshfs just for an indicator
            try:
                candidate = Path(item["workdir"]).expanduser().resolve()
                root = candidate if candidate.is_dir() else None
            except OSError:
                root = None
        targets.append((item, root, remote, root is not None))
    roots = [t[1] for t in targets if t[1] is not None] if available else []
    summaries = iter(await gitsync.pending_summaries(roots, refresh=refresh))
    projects = []
    for item, root, remote, exists in targets:
        # same condition as the roots filter above keeps both iterations aligned
        summary = next(summaries) if (root is not None and available) else gitsync.PendingSummary()
        projects.append({
            "workdir": item["workdir"], "name": item["name"],
            "workspace": item.get("workspace", ""), "remote": remote, "exists": exists,
            **summary.to_dict(),
        })
    return {"projects": projects, "session": False, "git_available": available}


async def list_branches(service: "ServerService", params: dict) -> dict:
    item, _, repo = await _resolve_repo(service, params)
    try:
        branches = await gitsync.list_branches(repo)
        st = await gitrepos.status(repo)
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc
    return {
        "project": item,
        "repo": repo_payload(repo, st, ""),
        "current": st.branch,
        "branches": [b.to_dict() for b in branches],
    }


async def push(service: "ServerService", payload: dict) -> dict:
    remote = _text(payload, "remote")
    set_upstream = _coerce_bool(payload.get("set_upstream"), False)
    return await _run_op(service, "push", payload,
                         lambda repo: gitsync.push(repo, set_upstream=set_upstream, remote=remote))


async def pull(service: "ServerService", payload: dict) -> dict:
    mode = _text(payload, "mode") or "ff-only"
    return await _run_op(service, "pull", payload, lambda repo: gitsync.pull(repo, mode=mode))


async def checkout(service: "ServerService", payload: dict) -> dict:
    branch = payload.get("branch")
    return await _run_op(service, "checkout", payload, lambda repo: gitsync.checkout(repo, branch))


async def abort(service: "ServerService", payload: dict) -> dict:
    return await _run_op(service, "abort", payload, gitsync.abort)
