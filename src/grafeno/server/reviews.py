"""Assisted review operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .. import gitrepos, gitsync, remotesession
from .. import reviews as reviews_module
from ..clisession_runtime import SessionBusy, SessionClosed
from ..i18n import t
from ..models import Task
from .actions import ApiError, _coerce_bool
from .actions import _load_or_404 as _load_task_or_404
from .clisessions import _manager, _require_manager, get_transcript, session_summary
from .projectdocs import resolve_project

if TYPE_CHECKING:
    from .service import ServerService

_STATUS = {
    "invalid_path": 400, "empty_message": 400, "multi_repo": 400,
    "not_pending": 409, "conflicted": 409, "not_committable": 409,
    "partially_staged": 409, "stale": 409, "operation_in_progress": 409,
    "commit_failed": 422, "git_failed": 500, "not_a_repo": 409,
    "timeout": 504, "git_missing": 503,
}


def _git_error(exc: gitrepos.GitReviewError) -> ApiError:
    message = f"{exc.message}: {exc.path}" if exc.path else exc.message
    return ApiError(_STATUS.get(exc.code, 400), message, code=exc.code)


def _guard() -> None:
    if remotesession.active():
        raise ApiError(409, "reviews are not available in remote session mode", code="remote")


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    return value.strip()


def _load_review(review_id: str) -> reviews_module.ReviewSession:
    try:
        return reviews_module.load(review_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise ApiError(404, "review not found") from exc


def _require_active(review: reviews_module.ReviewSession) -> None:
    if review.state == reviews_module.STATE_CLOSED:
        raise ApiError(409, "review is closed", code="closed")


async def _snapshot(review: reviews_module.ReviewSession) -> reviews_module.Snapshot:
    try:
        return await reviews_module.snapshot(review)
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc


def _file_ref(snap: reviews_module.Snapshot, params: dict) -> dict:
    """The snapshot entry designated by ``key`` or ``repo`` + ``path``."""
    key, repo, path = _text(params, "key"), _text(params, "repo"), _text(params, "path")
    entry = None
    if key:
        entry = next((e for e in snap.entries if e["key"] == key), None)
        label = key
    else:
        if not repo or not path:
            raise ApiError(400, "repo and path (or key) are required")
        entry = reviews_module.find_entry(snap, repo, path)
        label = f"{repo}/{path}"
    if entry is None:
        raise ApiError(409, f"file is not pending: {label}", code="not_pending")
    return entry


def _agent_running(service: "ServerService", review: reviews_module.ReviewSession) -> bool:
    manager = _manager(service)
    return bool(manager and review.agent_session_id and manager.is_running(review.agent_session_id))


def _publish(service: "ServerService", review: reviews_module.ReviewSession) -> None:
    publish = getattr(service, "publish", None)
    if publish is not None:
        publish("reviews", {"event": "review.changed", "review": review_summary(service, review)})


# --- serializers ---------------------------------------------------------------

def review_summary(service: "ServerService", review: reviews_module.ReviewSession) -> dict:
    return {
        "id": review.id,
        "name": review.name,
        "workdir": review.workdir,
        "project_name": Path(review.workdir).name or review.workdir,
        "task_id": review.task_id,
        "state": review.state,
        "cli": review.cli,
        "model": review.model,
        "effort": review.effort,
        "agent_session_id": review.agent_session_id,
        "agent_running": _agent_running(service, review),
        "cursor": {"repo": review.cursor_repo, "path": review.cursor_path}
        if review.cursor_path else None,
        "commits": review.commits,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }


def repo_payload(repo: gitrepos.Repo, status: gitrepos.RepoStatus | None, error: str) -> dict:
    return {
        "id": repo.id,
        "path": str(repo.path),
        "kind": repo.kind,
        "scope": repo.scope,
        "parent": repo.parent,
        "branch": status.branch if status else "",
        "head": status.head if status else "",
        "operation": status.operation if status else "",
        "files": len(status.files) if status else 0,
        "truncated": status.truncated if status else False,
        "hidden": list(status.hidden) if status else [],
        "upstream": status.upstream if status else "",
        "ahead": status.ahead if status else 0,
        "behind": status.behind if status else 0,
        "upstream_gone": status.upstream_gone if status else False,
        "error": error,
    }


def diff_payload(diff: gitrepos.FileDiff) -> dict:
    return {
        "text": diff.text, "binary": diff.binary, "truncated": diff.truncated,
        "size": diff.size, "staged_text": diff.staged_text,
        "unstaged_text": diff.unstaged_text,
    }


def _repos_payload(snap: reviews_module.Snapshot) -> list[dict]:
    return [repo_payload(r, snap.statuses.get(r.id), snap.errors.get(r.id, "")) for r in snap.repos]


def review_detail(service: "ServerService", review: reviews_module.ReviewSession,
                  snap: reviews_module.Snapshot) -> dict:
    cursor = (review.cursor_repo, review.cursor_path)
    current = reviews_module.navigate(review, snap, "current")
    if (review.cursor_repo, review.cursor_path) != cursor:
        # the cursor moved because its file vanished: keep the stored value untouched
        review.cursor_repo, review.cursor_path = cursor
    return {
        "review": review_summary(service, review),
        "repos": _repos_payload(snap),
        "files": snap.entries,
        "counts": reviews_module.counts(snap),
        "current": current,
    }


async def _entry_diff(snap: reviews_module.Snapshot, entry: dict,
                      max_bytes: int | None = None) -> dict:
    repo = reviews_module.repo_by_id(snap, entry["repo"])
    if repo is None:
        raise ApiError(409, "file is not pending", code="not_pending")
    change = reviews_module.change_of(snap, entry)
    has_head = bool(snap.statuses[entry["repo"]].head)
    try:
        if max_bytes is None:
            result = await gitrepos.diff(repo, change, has_head)
        else:
            result = await gitrepos.diff(repo, change, has_head, max_bytes=max_bytes)
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc
    return diff_payload(result)


def _max_bytes(params: dict) -> int | None:
    raw = params.get("max_bytes")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "max_bytes must be an integer") from exc


# --- operations ----------------------------------------------------------------

async def list_repos(service: "ServerService", workdir: str = "", project: str = "") -> dict:
    _guard()
    item, root = resolve_project(service, workdir, project)
    ephemeral = reviews_module.ReviewSession(id="", workdir=str(root))
    snap = await _snapshot(ephemeral)
    return {"project": item, "repos": _repos_payload(snap),
            "errors": dict(snap.errors)}


def list_reviews(service: "ServerService", workdir: str = "", state: str = "") -> dict:
    _guard()
    if state and state not in reviews_module.STATES:
        raise ApiError(400, "unsupported state")
    items = reviews_module.list_all(workdir, state)
    return {"reviews": [review_summary(service, r) for r in items]}


async def _prune_saved(review: reviews_module.ReviewSession,
                       snap: reviews_module.Snapshot) -> reviews_module.ReviewSession:
    """Drop decisions of files no longer pending, on a copy reloaded under the review lock."""
    async with reviews_module.review_lock(review.id):
        try:
            fresh = reviews_module.load(review.id)
        except (OSError, ValueError):
            return review  # deleted meanwhile: nothing to persist
        if reviews_module.prune(fresh, snap):
            reviews_module.save(fresh)
        return fresh


async def start_review(service: "ServerService", payload: dict) -> tuple[int, dict]:
    _guard()
    task_id = _text(payload, "task_id")
    if task_id:
        task: Task = _load_task_or_404(task_id)
        if task.is_remote:
            raise ApiError(409, "remote projects are not supported")
        workdir = task.workdir
    else:
        _, root = resolve_project(service, _text(payload, "workdir"), _text(payload, "project"))
        workdir = str(root)
    resume = _coerce_bool(payload.get("resume"), True)
    review = reviews_module.find_active(workdir, task_id) if resume else None
    resumed = review is not None
    if review is None:
        try:
            review = reviews_module.create(
                workdir, name=_text(payload, "name"), task_id=task_id,
                cli=_text(payload, "cli") or "claude", model=_text(payload, "model"),
                effort=_text(payload, "effort"),
            )
        except ValueError as exc:
            raise ApiError(400, str(exc)) from exc
    snap = await _snapshot(review)
    review = await _prune_saved(review, snap)
    body = review_detail(service, review, snap)
    body["resumed"] = resumed
    _publish(service, review)
    return (200 if resumed else 201), body


async def get_review(service: "ServerService", review_id: str) -> dict:
    _guard()
    review = _load_review(review_id)
    snap = await _snapshot(review)
    review = await _prune_saved(review, snap)
    return review_detail(service, review, snap)


def delete_review(service: "ServerService", review_id: str) -> dict:
    _guard()
    _load_review(review_id)
    reviews_module.delete(review_id)
    publish = getattr(service, "publish", None)
    if publish is not None:
        publish("reviews", {"event": "review.deleted", "review_id": review_id})
    return {"ok": True}


async def _set_state(service: "ServerService", review_id: str, state: str) -> dict:
    _guard()
    async with reviews_module.review_lock(review_id):  # serialized with navigate/decide/...
        review = _load_review(review_id)
        review.state = state
        reviews_module.save(review)
    _publish(service, review)
    return {"review": review_summary(service, review)}


async def close_review(service: "ServerService", review_id: str) -> dict:
    return await _set_state(service, review_id, reviews_module.STATE_CLOSED)


async def reopen_review(service: "ServerService", review_id: str) -> dict:
    return await _set_state(service, review_id, reviews_module.STATE_ACTIVE)


async def list_files(service: "ServerService", review_id: str, repo: str = "",
                     decision: str = "") -> dict:
    _guard()
    review = _load_review(review_id)
    snap = await _snapshot(review)
    files = [
        e for e in snap.entries
        if (not repo or e["repo"] == repo) and (not decision or e["decision"] == decision)
    ]
    return {"files": files, "counts": reviews_module.counts(snap)}


async def get_diff(service: "ServerService", review_id: str, params: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    snap = await _snapshot(review)
    entry = _file_ref(snap, params)
    return {"file": entry, "diff": await _entry_diff(snap, entry, _max_bytes(params))}


async def _with_current(snap: reviews_module.Snapshot, current: dict | None,
                        payload: dict) -> dict:
    body: dict = {"current": current}
    if current is not None and _coerce_bool(payload.get("diff"), True):
        body["diff"] = await _entry_diff(snap, current, _max_bytes(payload))
    return body


async def _navigate(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    snap = await _snapshot(review)
    action = _text(payload, "action")
    repo, path = _text(payload, "repo"), _text(payload, "path")
    if _text(payload, "key"):
        repo_path = _file_ref(snap, payload)
        repo, path = repo_path["repo"], repo_path["path"]
    try:
        current = reviews_module.navigate(
            review, snap, action, repo, path,
            skip_decided=_coerce_bool(payload.get("skip_decided"), False),
        )
    except ValueError as exc:
        status = 409 if str(exc) == "file not pending" else 400
        raise ApiError(status, str(exc), code="not_pending" if status == 409 else "") from exc
    reviews_module.save(review)
    body = await _with_current(snap, current, payload)
    body["end"] = current is None and (
        action in ("next", "prev") or (action == "first" and bool(snap.entries)))
    body["counts"] = reviews_module.counts(snap)
    _publish(service, review)
    return body


async def _decide(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    snap = await _snapshot(review)
    entry = _file_ref(snap, payload)
    try:
        reviews_module.decide(review, snap, entry["repo"], entry["path"],
                              _text(payload, "decision"), _text(payload, "note"))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    body: dict = {"file": reviews_module.find_entry(snap, entry["repo"], entry["path"])}
    if _coerce_bool(payload.get("advance"), False):
        # advance from the file just decided, not from wherever the cursor was
        review.cursor_repo, review.cursor_path = entry["repo"], entry["path"]
        current = reviews_module.navigate(
            review, snap, "next", skip_decided=_coerce_bool(payload.get("skip_decided"), False))
        body.update(await _with_current(snap, current, payload))
        body["end"] = current is None
    reviews_module.save(review)
    body["counts"] = reviews_module.counts(snap)
    _publish(service, review)
    return body


async def _agent_turn(service: "ServerService", review: reviews_module.ReviewSession,
                      call) -> dict:
    manager = _require_manager(service)
    try:
        session = await call(manager)
    except SessionBusy as exc:
        raise ApiError(409, t("cs.busy"), code="busy") from exc
    except SessionClosed as exc:
        raise ApiError(409, t("cs.closed_notice"), code="closed") from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc
    _publish(service, review)
    return {
        "session": session_summary(manager.get(session.id), manager.is_running(session.id)),
        "review": review_summary(service, review),
    }


async def _discuss(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    _require_manager(service)
    snap = await _snapshot(review)
    has_ref = bool(_text(payload, "key") or _text(payload, "path"))
    entry = _file_ref(snap, payload) if has_ref else None
    message = _text(payload, "message")
    return await _agent_turn(
        service, review,
        lambda manager: reviews_module.discuss(manager, review, snap, entry, message))


async def _request_changes(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    _require_manager(service)
    snap = await _snapshot(review)
    entry = _file_ref(snap, payload)
    instructions = _text(payload, "instructions")
    body = await _agent_turn(
        service, review,
        lambda manager: reviews_module.request_changes(manager, review, snap, entry, instructions))
    body["file"] = {"repo": entry["repo"], "path": entry["path"],
                    "decision": "changes_requested", "note": instructions}
    return body


async def _locked(call, service: "ServerService", review_id: str, payload: dict) -> dict:
    """Run a load -> mutate -> save operation serialized per review (no lost updates)."""
    async with reviews_module.review_lock(review_id):
        return await call(service, review_id, payload)


async def navigate(service: "ServerService", review_id: str, payload: dict) -> dict:
    return await _locked(_navigate, service, review_id, payload)


async def decide(service: "ServerService", review_id: str, payload: dict) -> dict:
    return await _locked(_decide, service, review_id, payload)


async def discuss(service: "ServerService", review_id: str, payload: dict) -> dict:
    return await _locked(_discuss, service, review_id, payload)


async def request_changes(service: "ServerService", review_id: str, payload: dict) -> dict:
    return await _locked(_request_changes, service, review_id, payload)


def review_transcript(service: "ServerService", review_id: str, after: int = 0,
                      limit: int = 0) -> dict:
    _guard()
    review = _load_review(review_id)
    if not review.agent_session_id:
        return {"entries": [], "total": 0, "running": False, "session": None}
    return get_transcript(service, review.agent_session_id, after=after, limit=limit)


def cancel_review_turn(service: "ServerService", review_id: str) -> dict:
    _guard()
    review = _load_review(review_id)
    manager = _require_manager(service)
    if not review.agent_session_id or not manager.cancel(review.agent_session_id):
        raise ApiError(409, "agent is not running")
    session = manager.get(review.agent_session_id)
    return {"session": session_summary(session, manager.is_running(session.id))}


def _file_refs(snap: reviews_module.Snapshot, payload: dict) -> list[dict]:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ApiError(400, "files must be a non-empty list")
    refs: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            raise ApiError(400, "each file must be an object")
        entry = _file_ref(snap, item)
        refs.append({"repo": entry["repo"], "path": entry["path"],
                     "fingerprint": _text(item, "fingerprint")})
    return refs


async def commit_message(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    snap = await _snapshot(review)
    refs = _file_refs(snap, payload)
    repos = {r["repo"] for r in refs}
    if len(repos) > 1:
        raise ApiError(400, "files belong to several repositories: " + ", ".join(sorted(repos)),
                       code="multi_repo")
    try:
        return await reviews_module.suggest_commit_message(
            review, snap, refs[0]["repo"], [r["path"] for r in refs],
            hint=_text(payload, "hint"), cli=_text(payload, "cli"),
            model=_text(payload, "model"), effort=_text(payload, "effort"),
        )
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc
    except TimeoutError as exc:
        raise ApiError(504, "commit message generation timed out", code="timeout") from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    except RuntimeError as exc:
        raise ApiError(502, str(exc), code="agent_failed") from exc


async def commit(service: "ServerService", review_id: str, payload: dict) -> dict:
    _guard()
    review = _load_review(review_id)
    _require_active(review)
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        raise ApiError(400, "message is required", code="empty_message")
    snap = await _snapshot(review)
    refs = _file_refs(snap, payload)
    try:
        result = await reviews_module.commit_files(
            review, snap, refs, message,
            include_unstaged=_coerce_bool(payload.get("include_unstaged"), False),
            allow_multi_repo=_coerce_bool(payload.get("allow_multi_repo"), False),
        )
    except gitrepos.GitReviewError as exc:
        raise _git_error(exc) from exc
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    first_error = result["errors"][0] if result["errors"] else None
    if first_error and not result["commits"]:
        raise ApiError(_STATUS.get(first_error["code"], 400),
                       first_error["message"] + (f": {first_error['path']}" if first_error["path"] else ""),
                       code=first_error["code"])
    if result["commits"]:
        gitsync.invalidate(Path(review.workdir))
    log = getattr(service, "_log", None)
    for record in result["commits"]:
        if log is not None:
            log(f"review {review.id}: committed {record['hash'][:7]} in {record['repo']}")
    _publish(service, review)
    return {
        "commits": result["commits"],
        "errors": result["errors"],
        "partial": result["partial"],
        "review": review_detail(service, review, result["snapshot"]),
    }


def list_commits(service: "ServerService", review_id: str) -> dict:
    _guard()
    _load_review(review_id)
    return {"commits": reviews_module.load_commits(review_id)}
