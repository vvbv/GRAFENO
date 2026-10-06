"""Read/write operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .. import media, models, paths, profiles as profiles_module, remote, remotesession, scheduler
from ..config import KNOWN_CLIS, RoleConfig
from ..i18n import t
from ..mdnorm import normalize_markdown
from ..models import Task, TaskState, task_state_label
from ..pipeline.hooks import HOOK_STAGES, format_stages, parse_stages
from ..pipeline.orchestrator import INTERRUPTED_PHASE, phase_label
from ..references import Reference
from ..telegram import stt, tts

if TYPE_CHECKING:
    from .service import ServerService


MAX_CREATE_ATTACHMENTS = 10  # decoded entries per create call
AUDIO_TRANSCRIPTION_HEADER = "Transcription of audio attachment"  # description section per audio file


class ApiError(Exception):
    """Domain error raised by action handlers; mapped to an HTTP status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


ROLE_NAMES = ("first", "planner", "implementer", "reviewer", "final")
RUN_PHASES = ("plan", "implement", "review", "fix", "final", "tests", "automode")
_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg", ".mp3": "audio/mpeg",
    ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".webm": "video/webm",
}


def _runtime_of(service: "ServerService", task_id: str):
    """Existing runtime of a task in the App (never creates one)."""
    app = service.app
    runtimes = getattr(app, "runtimes", None) if app is not None else None
    return runtimes.get(task_id) if isinstance(runtimes, dict) else None


def _is_running(service: "ServerService", task_id: str) -> bool:
    runtime = _runtime_of(service, task_id)
    return bool(runtime is not None and runtime.running)


def _live(service: "ServerService", task: Task) -> Task:
    """While a pipeline runs, its in-memory task is fresher than the disk copy
    (transient fields such as ``usage_waiting`` only live there)."""
    runtime = _runtime_of(service, task.id)
    if runtime is not None and runtime.running and runtime.task.id == task.id:
        return runtime.task
    return task


def task_summary(task: Task, running: bool = False) -> dict:
    """Slim task payload for list endpoints and WS events."""
    total_in, total_out = task.token_totals()
    return {
        "id": task.id,
        "name": task.name,
        "workdir": task.workdir,
        "remote": task.remote,
        "state": task.state.value,
        "state_label": task_state_label(task),
        "automode": task.automode,
        "origin": task.origin,
        "profile": task.profile,
        "scheduled_at": task.scheduled_at,
        "parent_id": task.parent_id,
        "iteration": task.iteration,
        "cycle": task.cycle,
        "repeat_mode": task.repeat_mode,
        "usage_waiting": task.usage_waiting,
        "running": running,
        "tokens": {"input": total_in, "output": total_out},
        "tokens_by_agent": {
            label: [pair_in, pair_out]
            for label, (pair_in, pair_out) in task.tokens_by_cli_model().items()
        },
        "duration_seconds": task.total_duration_seconds(),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def task_detail(task: Task) -> dict:
    """Full payload: to_dict() + derived data (label, token totals)."""
    data = task.to_dict()
    data["state_label"] = task_state_label(task)
    total_in, total_out = task.token_totals()
    data["token_totals"] = {"input": total_in, "output": total_out}
    data["total_duration_seconds"] = task.total_duration_seconds()
    return data


# ---------------------------------------------------------------------- #
# Read operations
# ---------------------------------------------------------------------- #
def list_tasks(service: "ServerService", state: str | None = None) -> dict:
    """List tasks, optionally filtered by state.

    Tasks come in chain order (each child right after its parent, with its
    ``depth``), like the TUI list. ``done_hidden_ids`` are the finished
    tasks the "hide completed" filter may hide without breaking a chain.
    """
    all_tasks = [_live(service, task) for task in models.list_all()]
    tasks = all_tasks
    if state:
        try:
            wanted = TaskState(state)
        except ValueError as exc:
            raise ApiError(400, f"unknown state: {state}") from exc
        tasks = [task for task in tasks if task.state is wanted]
    items = []
    for task, depth in scheduler.tree_order(tasks):
        item = task_summary(task, running=_is_running(service, task.id))
        item["depth"] = depth
        items.append(item)
    return {
        "tasks": items,
        "done_hidden_ids": sorted(scheduler.done_hidden_ids(all_tasks)),
    }


def get_task(service: "ServerService", task_id: str) -> dict:
    """Return the full payload of one task.

    The plan's contract is ``{"task": task_detail(task)}``: the response
    is wrapped under a ``task`` key (symmetric with ``list_tasks`` which
    wraps each item in ``{"tasks": [...]}``). ``task_detail`` itself
    already contains a nested ``task`` key (it returns ``Task.to_dict()``
    plus a few derived fields), so the result intentionally has a
    ``task.task.id`` location for the identifier; this matches the plan.
    """
    task = _live(service, _load_or_404(task_id))
    data = task_detail(task)
    data.update(_detail_extras(service, task))
    return {"task": data}


def _artifact_index(task_id: str) -> dict[str, list[str]]:
    """Relative paths of every .md of each phase (all cycles), like the TUI."""
    index: dict[str, list[str]] = {}
    for kind in _KINDS:
        root = paths.task_dir(task_id) / kind
        index[kind] = (
            sorted(str(entry.relative_to(root)) for entry in root.glob("**/*.md"))
            if root.is_dir() else []
        )
    return index


def _media_names(task_id: str) -> list[str]:
    directory = paths.task_dir(task_id) / "media"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if entry.is_file())


def _phases(task: Task) -> list[str]:
    """Phases of the progress bar (first step only when the task defines it)."""
    phases = list(models.PHASES)
    if task.first_prompt.strip():
        phases.insert(0, "first")
    return phases


def _agents(task: Task) -> list[dict]:
    """Agent (cli/model) and consumption per phase (TUI agents bar)."""
    by_phase = task.tokens_by_phase()
    phases = ["plan", "implement", "review"]
    if "fix" in by_phase:
        phases.append("fix")  # fix uses the implementer role; only if there were fixes
    phases.append("final")
    if task.first_prompt.strip():
        phases.insert(0, "first")
    role_of = {"first": "first", "plan": "planner", "implement": "implementer",
               "review": "reviewer", "fix": "implementer", "final": "final"}
    result = []
    for phase in phases:
        role = task.role(role_of[phase])
        pair_in, pair_out = by_phase.get(phase, (0, 0))
        result.append({
            "phase": phase,
            "label": phase_label(phase),
            "cli": role.cli,
            "model": role.model,
            "effort": role.effort,
            "tokens": {"input": pair_in, "output": pair_out} if phase in by_phase else None,
        })
    return result


def _missing_models(service: "ServerService", task: Task) -> list[str]:
    from .. import modelcheck

    available = getattr(service.app, "available_models", None) or {}
    if not isinstance(available, dict):
        return []
    issues = modelcheck.find_missing(modelcheck.collect_task_roles(task), available)
    return modelcheck.format_issues(issues)


def _runtime_info(service: "ServerService", task: Task) -> dict:
    import time

    runtime = _runtime_of(service, task.id)
    running = bool(runtime is not None and runtime.running)
    info = {
        "running": running,
        "phase_label": "",
        "phase_elapsed_seconds": 0,
        "silence_seconds": 0,
        "event_count": 0,
        "pending_plan_confirm": bool(runtime is not None and runtime.pending_plan_confirm),
        "total_seconds": task.total_duration_seconds(),
    }
    if running and runtime.phase_started_at is not None:
        now = time.monotonic()
        elapsed = now - runtime.phase_started_at
        info.update(
            phase_label=runtime.phase_label,
            phase_elapsed_seconds=int(elapsed),
            silence_seconds=int(now - runtime.last_activity),
            event_count=runtime.event_count,
            total_seconds=int(task.total_duration_seconds() + elapsed),
        )
    return info


def available_actions(service: "ServerService", task: Task) -> dict[str, bool]:
    """Which detail actions apply right now (same rules as the TUI detail)."""
    running = _is_running(service, task.id)
    discarded = task.state is TaskState.DISCARDED
    idle = not running and not discarded
    plan_files = any(paths.plan_dir(task.id, task.cycle).glob("*.md"))
    review_files = any(paths.review_dir(task.id, task.cycle).glob("*.md"))
    return {
        "plan": idle,
        "implement": idle and plan_files,
        "review": idle and task.state in {TaskState.IMPLEMENTED, TaskState.PAUSED, TaskState.FAILED},
        "fix": idle and (task.iteration > 0 or review_files),
        "final": idle and task.state is TaskState.DONE,
        "tests": idle and bool(task.test_command.strip()),
        "automode": idle,
        "approve_plan": idle and task.state is TaskState.PLANNED and plan_files,
        "extend": idle,
        "resume": idle and task.state is TaskState.FAILED,
        "continue": idle and (task.state is TaskState.FAILED or task.state in INTERRUPTED_PHASE),
        "reset": not discarded,
        "cancel": running,
        "edit": not running,
        "roles": not running,
        "mark_done": idle and task.state is not TaskState.DONE,
        "discard": not running and not discarded,
    }


def _detail_extras(service: "ServerService", task: Task) -> dict:
    """Derived data the web panel shows next to ``task_detail``."""
    all_tasks = models.list_all()
    current = task.parent_id
    candidates = [
        {"id": item.id, "name": item.name}
        for item in scheduler.rechain_candidates(task, all_tasks)
    ]
    if current and all(item["id"] != current for item in candidates):
        parent = next((item for item in all_tasks if item.id == current), None)
        candidates.append({"id": current, "name": parent.name if parent else current})
    tokens_by_phase = task.tokens_by_phase()
    ordered = sorted(
        tokens_by_phase.items(),
        key=lambda item: (
            models.TOKEN_PHASES.index(item[0])
            if item[0] in models.TOKEN_PHASES else len(models.TOKEN_PHASES)
        ),
    )
    from ..tui.widgets import _phase_order, _phase_status

    has_first = bool(task.first_prompt.strip())
    status = _phase_status(task.state, has_first=has_first)
    return {
        "phases": _phases(task),
        "phase_bar": [
            {"key": key, "label": t(label_key), "status": status.get(key, "pending")}
            for key, label_key in _phase_order(has_first)
        ],
        "usage_waiting": task.usage_waiting,
        "agents": _agents(task),
        "tokens_by_phase": [
            {"phase": phase, "label": t(f"phase.{phase}"), "input": pair_in, "output": pair_out}
            for phase, (pair_in, pair_out) in ordered
        ],
        "tokens_by_agent": [
            {"label": label, "input": pair_in, "output": pair_out}
            for label, (pair_in, pair_out) in sorted(
                task.tokens_by_cli_model().items(),
                key=lambda item: (-(item[1][0] + item[1][1]), item[0]),
            )
        ],
        "runtime": _runtime_info(service, task),
        "actions": available_actions(service, task),
        "artifacts": _artifact_index(task.id),
        "media": _media_names(task.id),
        "models_missing": _missing_models(service, task),
        "rechain_candidates": candidates,
        "remote_target": remotesession.describe_target(task) if (task.is_remote or remotesession.active()) else "",
    }


def list_projects(service: "ServerService") -> dict:
    """Distinct task workdirs with their task counts (best effort)."""
    counts: dict[str, int] = {}
    for task in models.list_all():
        if not task.workdir:
            continue
        counts[task.workdir] = counts.get(task.workdir, 0) + 1
    try:
        from .. import workspaces as workspaces_module

        discovered = [
            str(path)
            for path in workspaces_module.discover(workspaces_module.resolve([]))
        ]
    except Exception:  # noqa: BLE001 - workspaces discovery is best effort
        discovered = []
    seen: set[str] = set()
    items: list[dict] = []
    for workdir, count in sorted(counts.items()):
        seen.add(workdir)
        items.append({"workdir": workdir, "count": count})
    for workdir in sorted(set(discovered) - seen):
        items.append({"workdir": workdir, "count": 0})
    return {"projects": items}


def get_logs(service: "ServerService", task_id: str, limit: int = 200) -> dict:
    """Tail of the persisted live log of a task (plain text per line)."""
    if limit < 1 or limit > 1000:
        raise ApiError(400, "limit must be between 1 and 1000")
    # Ensure the task exists: 404 before any content.
    try:
        models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    from .. import live_log

    entries = live_log.load(task_id, limit)
    lines = [entry.plain for entry in entries]
    return {
        "logs": lines,
        "entries": [{"text": entry.plain, "style": str(entry.style or "")} for entry in entries],
    }


_KINDS = {
    "first": paths.first_dir,
    "plan": paths.plan_dir,
    "review": paths.review_dir,
    "final": paths.final_dir,
}


def get_artifacts(service: "ServerService", task_id: str, kind: str, cycle: int = 1) -> dict:
    """Return every .md file of a first/plan/review/final phase of a task."""
    if kind not in _KINDS:
        raise ApiError(400, f"unknown kind: {kind}")
    if cycle < 1:
        raise ApiError(400, "cycle must be >= 1")
    try:
        models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    directory: Path = _KINDS[kind](task_id, cycle)
    files: list[dict] = []
    if directory.is_dir():
        for entry in sorted(directory.glob("*.md")):
            try:
                content = entry.read_text(encoding="utf-8")
            except OSError:
                continue
            files.append({"name": entry.name, "content": content})
    return {"kind": kind, "cycle": cycle, "files": files}


def get_artifact_file(service: "ServerService", task_id: str, kind: str, path: str) -> dict:
    """One .md of a phase by its path relative to the phase root (any cycle).

    The content is normalized like the TUI viewer (``normalize_markdown``).
    """
    if kind not in _KINDS:
        raise ApiError(400, f"unknown kind: {kind}")
    _load_or_404(task_id)
    root = (paths.task_dir(task_id) / kind).resolve()
    target = (root / path).resolve()
    if not path or not target.is_relative_to(root) or target.suffix.lower() != ".md":
        raise ApiError(400, "invalid path")
    if not target.is_file():
        raise ApiError(404, "file not found")
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApiError(404, "file not found") from exc
    return {"kind": kind, "path": path, "content": normalize_markdown(content)}


def get_media(service: "ServerService", task_id: str, name: str) -> tuple[bytes, str]:
    """Raw bytes and MIME type of one file of the task's media dir."""
    _load_or_404(task_id)
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ApiError(400, "invalid name")
    target = paths.task_dir(task_id) / "media" / name
    if not target.is_file():
        raise ApiError(404, "media not found")
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise ApiError(404, "media not found") from exc
    return data, _MEDIA_TYPES.get(target.suffix.lower(), "application/octet-stream")


# ---------------------------------------------------------------------- #
# Write operations
# ---------------------------------------------------------------------- #
def _runtime_start(
    service: "ServerService",
    task: Task,
    runner_factory,
    label: str,
    plan_then_ask: bool = False,
) -> None:
    """Start a runner on the task's runtime, raising ApiError on failures."""
    app = service.app
    if app is None:
        raise ApiError(503, "app unavailable")
    runtime = service.app.runtime_for(task)
    if not runtime.start(app, runner_factory, label, plan_then_ask=plan_then_ask):
        raise ApiError(409, "task already running")


def _pipeline_runner(task: Task):
    """Full pipeline respecting ``confirm_plan`` (same as the TUI detail):
    returns ``(runner, label, plan_then_ask)``."""
    if task.confirm_plan:
        return (lambda orch: orch.run_automode_plan()), t("det.cycle_plan", n=task.cycle), True
    return (lambda orch: orch.run_automode()), t("det.cycle_auto", n=task.cycle), False


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a JSON value to a boolean.

    Truthy strings (``"true"``, ``"1"``, ``"yes"``) and actual booleans
    become True; the rest become False. Without this, ``bool("false")``
    would silently keep automode on.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _decode_attachments(payload: dict) -> list[tuple[str, bytes]]:
    """Validate the optional ``attachments`` list of a create payload.

    Each entry must be an object with ``data`` (base64 string) and an
    optional ``name`` (defaults to "attachment"). Raises ``ApiError(400)``
    on any malformed entry; the body cap (MAX_BODY, 8 MiB) already bounds
    the total size.
    """
    raw = payload.get("attachments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError(400, "attachments must be a list")
    if len(raw) > MAX_CREATE_ATTACHMENTS:
        raise ApiError(400, f"too many attachments (max {MAX_CREATE_ATTACHMENTS})")
    decoded: list[tuple[str, bytes]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("data"), str):
            raise ApiError(400, f"attachment {index} must be an object with 'data'")
        name = str(item.get("name") or "attachment")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError(400, f"attachment {index}: invalid base64 data") from exc
        decoded.append((name, data))
    return decoded


async def _transcribe_audio_attachments(
    service: "ServerService",
    telegram_cfg,
    attachments: list[tuple[str, bytes]],
) -> list[dict]:
    """Transcribe audio attachments with the configured STT provider.

    Best effort: an entry without ``text`` means the audio was kept as a
    file but could not be transcribed (reason under ``error``). Never
    raises; failures are logged to the API log.
    """
    key = telegram_cfg.resolve_stt_key()
    if not key:
        return [{"name": name, "error": "stt not configured"} for name, _ in attachments]
    results: list[dict] = []
    for name, data in attachments:
        reasons: list[str] = []
        text = await asyncio.to_thread(
            stt.transcribe,
            url=telegram_cfg.stt_url,
            api_key=key,
            model=telegram_cfg.stt_model,
            data=data,
            filename=name or "audio.ogg",
            on_error=reasons.append,
        )
        if text:
            results.append({"name": name, "text": text})
        else:
            reason = reasons[0] if reasons else "unknown"
            service._log(f"stt failed for attachment {name!r}: {reason}")
            results.append({"name": name, "error": reason})
    return results


def _optional_str(payload: dict, key: str) -> str | None:
    """String field or ``None`` when absent (the config default applies)."""
    value = payload.get(key)
    return None if value is None else str(value).strip()


def _optional_bool(payload: dict, key: str) -> bool | None:
    value = payload.get(key)
    return None if value is None else _coerce_bool(value, False)


def _parse_references(raw: object) -> list[Reference] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ApiError(400, "references must be a list")
    result: list[Reference] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "each reference must be an object")
        ref = Reference.from_dict(item)
        if ref.name.strip() and ref.path.strip():
            result.append(ref)
    return result


def _parse_hook_stages(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return format_stages([str(item) for item in raw])
    return format_stages(parse_stages(str(raw)))


def _resolve_location(payload: dict) -> tuple[str, str]:
    """``(workdir, remote_spec)`` validated like the TUI new-task form."""
    remote_text = str(payload.get("remote") or "").strip()
    raw_dir = str(payload.get("workdir") or "").strip()
    if remotesession.active():
        # Session mode: an absolute path ON THE REMOTE HOST, not validated here.
        if not raw_dir or not (raw_dir.startswith("/") or raw_dir.startswith("~")):
            raise ApiError(400, t("nt.error.bad_remote_dir"))
        return raw_dir, ""
    if remote_text:
        spec = remote.parse_spec(remote_text)
        if spec is None:
            raise ApiError(400, t("nt.error.bad_remote"))
        return spec.path, spec.canonical
    if not raw_dir:
        raise ApiError(400, "workdir is required")
    workdir = Path(raw_dir).expanduser()
    if not workdir.is_dir():
        raise ApiError(400, t("nt.error.bad_dir", path=workdir))
    return str(workdir.resolve()), ""


async def create_task(service: "ServerService", payload: dict) -> dict:
    """Create a new task from a JSON payload and return its summary.

    Accepts every option of the TUI new-task form; absent fields take the
    global config defaults exactly like ``Task.create``.
    """
    from .. import config as config_module

    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be an object")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    workdir, remote_spec = _resolve_location(payload)
    description = str(payload.get("description") or "")
    attachments = _decode_attachments(payload)  # 400 before creating the task
    cfg = config_module.load()
    parent_id = str(payload.get("parent_id") or "").strip()
    profile_name = str(payload.get("profile") or "").strip()
    profile_obj = None
    if profile_name:
        profile_obj = profiles_module.find(profile_name)
        if profile_obj is None:
            raise ApiError(400, f"unknown profile: {profile_name}")
    automode = _coerce_bool(payload.get("automode"), True)
    repeat_mode = str(payload.get("repeat_mode") or "").strip()
    if repeat_mode not in ("", "interval", "infinite"):
        raise ApiError(400, "repeat_mode must be interval or infinite")
    repeat_minutes = 60
    if repeat_mode == "interval":
        try:
            repeat_minutes = int(payload.get("repeat_interval_minutes") or 0)
        except (TypeError, ValueError):
            repeat_minutes = 0
        if repeat_minutes < 1:
            raise ApiError(400, t("nt.error.bad_interval"))
    if repeat_mode:
        automode = True  # repetitive tasks always run in automode (TUI rule)
    plan_reuse = str(payload.get("plan_reuse") or "reuse").strip()
    if plan_reuse not in ("reuse", "replan", "reevaluate"):
        raise ApiError(400, "plan_reuse must be reuse, replan or reevaluate")
    hook_mode = str(payload.get("hook_mode") or "override").strip()
    if hook_mode not in ("override", "both"):
        raise ApiError(400, "hook_mode must be override or both")
    try:
        scheduled_at = scheduler.parse_schedule(str(payload.get("scheduled_at") or ""))
    except ValueError as exc:
        raise ApiError(400, t("nt.error.bad_schedule")) from exc
    task = models.Task.create(
        name=name,
        description=description,
        workdir=workdir,
        config=cfg,
        remote=remote_spec,
        automode=automode,
        test_command=_optional_str(payload, "test_command"),
        create_branch=_optional_bool(payload, "create_branch"),
        confirm_plan=_optional_bool(payload, "confirm_plan"),
        first_prompt=_optional_str(payload, "first_prompt"),
        final_prompt=_optional_str(payload, "final_prompt"),
        hook_command=_optional_str(payload, "hook_command"),
        hook_stages=_parse_hook_stages(payload.get("hook_stages")),
        hook_mode=hook_mode,
        scheduled_at=scheduled_at,
        parent_id=parent_id or None,
        repeat_mode=repeat_mode,
        repeat_interval_minutes=repeat_minutes,
        plan_reuse=plan_reuse,
        use_global_references=_optional_bool(payload, "use_global_references"),
        use_project_references=_optional_bool(payload, "use_project_references"),
        references=_parse_references(payload.get("references")),
        profile=profile_obj,
    )
    if parent_id:
        # Creation mirrors the TUI new-task form: the parent only has to
        # exist (a completed parent is allowed; the stricter position rules
        # apply when re-chaining later via edit_task).
        by_id = {item.id: item for item in models.list_all()}
        if parent_id not in by_id:
            raise ApiError(400, t("et.error.parent_missing"))
        if parent_id == task.id:
            raise ApiError(400, t("et.error.parent_self"))
        if not task.scheduled_at:
            # Due as soon as the parent is DONE (right away when it already
            # finished): the scheduler tick starts it unattended.
            task.scheduled_at = datetime.now().isoformat(timespec="minutes")
    if payload.get("origin"):
        task.origin = str(payload["origin"])
    else:
        task.origin = "api"
    models.save(task)
    if attachments:
        media.attach_files(task, attachments, header="Attachments received via the API:")
    transcriptions: list[dict] = []
    audio_attachments = [(name, data) for name, data in attachments if media.is_audio_name(name)]
    if audio_attachments:
        transcriptions = await _transcribe_audio_attachments(service, cfg.telegram, audio_attachments)
        for item in transcriptions:
            if item.get("text"):
                task.description += (
                    f"\n\n{AUDIO_TRANSCRIPTION_HEADER} '{item['name']}':\n{item['text']}\n"
                )
        if any(item.get("text") for item in transcriptions):
            models.save(task)
    return 201, {"task": task_summary(task), "transcriptions": transcriptions}


def _load_or_404(task_id: str) -> Task:
    try:
        return models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc


def start_task(service: "ServerService", task_id: str) -> dict:
    """Start the full automode pipeline of a task (pausing after the plan
    when the task asks for plan confirmation, like the TUI)."""
    task = _load_or_404(task_id)
    runner, label, plan_then_ask = _pipeline_runner(task)
    _runtime_start(service, task, runner, label, plan_then_ask)
    return {"ok": True}


def _ensure_idle(service: "ServerService", task: Task) -> None:
    """Shared guard of the pipeline actions (TUI detail rules)."""
    if _is_running(service, task.id):
        raise ApiError(409, t("api.err.running"))
    if task.state is TaskState.DISCARDED:
        raise ApiError(409, t("det.warn.discarded"))


def run_phase(service: "ServerService", task_id: str, phase: str) -> dict:
    """Run one pipeline phase (or the full automode) with the TUI checks."""
    task = _load_or_404(task_id)
    if phase not in RUN_PHASES:
        raise ApiError(400, f"unknown phase: {phase}")
    _ensure_idle(service, task)
    if phase == "automode":
        runner, label, plan_then_ask = _pipeline_runner(task)
        _runtime_start(service, task, runner, label, plan_then_ask)
        return {"ok": True}
    if phase == "implement" and not any(paths.plan_dir(task.id, task.cycle).glob("*.md")):
        raise ApiError(409, t("api.err.need_plan"))
    if phase == "review" and task.state not in {TaskState.IMPLEMENTED, TaskState.PAUSED, TaskState.FAILED}:
        raise ApiError(409, t("api.err.need_impl"))
    if phase == "fix" and task.iteration == 0 and not any(paths.review_dir(task.id, task.cycle).glob("*.md")):
        raise ApiError(409, t("api.err.need_review"))
    if phase == "final" and task.state is not TaskState.DONE:
        raise ApiError(409, t("api.err.need_done"))
    if phase == "tests":
        if not task.test_command.strip():
            raise ApiError(409, t("det.warn.no_tests"))
        if service.app is None:
            raise ApiError(503, "app unavailable")
        runtime = service.app.runtime_for(task)

        async def _tests(orch) -> None:
            ok = await orch.run_tests()
            runtime._cb_info(t("det.tests.ok") if ok else t("det.tests.fail"))

        _runtime_start(service, task, _tests, phase_label("tests"))
        return {"ok": True}
    method = {"plan": "run_plan", "implement": "run_implement", "review": "run_review",
              "fix": "run_fix", "final": "run_final"}[phase]
    _runtime_start(service, task, lambda orch: getattr(orch, method)(), phase_label(phase))
    return {"ok": True}


def continue_task(service: "ServerService", task_id: str) -> dict:
    """Continue a FAILED or interrupted task from the phase where it stopped."""
    task = _load_or_404(task_id)
    _ensure_idle(service, task)
    if task.state is not TaskState.FAILED and task.state not in INTERRUPTED_PHASE:
        raise ApiError(409, t("api.err.not_interrupted"))
    _runtime_start(service, task, lambda orch: orch.run_continue(), t("det.continue.label"))
    return {"ok": True}


def approve_plan(service: "ServerService", task_id: str) -> dict:
    """Plan confirmation point: implement + review loop on the current plan."""
    task = _load_or_404(task_id)
    _ensure_idle(service, task)
    if task.state is not TaskState.PLANNED:
        raise ApiError(409, t("api.err.not_planned"))
    runtime = _runtime_of(service, task.id)
    if runtime is not None:
        runtime.pending_plan_confirm = False
    _runtime_start(service, task, lambda orch: orch.run_automode_continue(), t("det.automode_impl"))
    return {"ok": True}


async def _cancel_and_wait(service: "ServerService", task: Task) -> None:
    """Cancel a running pipeline and wait (max 10 s) for the worker to stop."""
    runtime = _runtime_of(service, task.id)
    if runtime is None or not runtime.running:
        return
    runtime.cancel()
    for _ in range(100):  # 100 x 0.1s = 10s budget
        await asyncio.sleep(0.1)
        if not runtime.running:
            return
    raise ApiError(409, t("api.err.running"))


async def reset_task(service: "ServerService", task_id: str) -> dict:
    """Abort any running execution and reset the task to DRAFT (TUI ``R``)."""
    task = _load_or_404(task_id)
    if task.state is TaskState.DISCARDED:
        raise ApiError(409, t("det.warn.discarded"))
    await _cancel_and_wait(service, task)
    task = _load_or_404(task_id)
    models.reset_to_draft(task)
    runtime = _runtime_of(service, task.id)
    if runtime is not None:
        runtime.task = task
        runtime.log.clear()
        runtime._cb_info(t("det.restarted"))
    return {"ok": True, "state": task.state.value}


def edit_task(service: "ServerService", task_id: str, payload: dict) -> dict:
    """Edit name, description and/or chain parent (TUI edit modal)."""
    task = _load_or_404(task_id)
    if _is_running(service, task.id):
        raise ApiError(409, t("api.err.running"))
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ApiError(400, t("et.error.name_required"))
        task.name = name
    if "description" in payload:
        task.description = str(payload.get("description") or "").strip()
    if "parent_id" in payload:
        parent_id = str(payload.get("parent_id") or "").strip()
        if parent_id != task.parent_id:
            by_id = {item.id: item for item in models.list_all()}
            error = scheduler.rechain_error(task, parent_id, by_id)
            if error:
                raise ApiError(400, t(error))
            task.parent_id = parent_id
    models.save(task)
    runtime = _runtime_of(service, task.id)
    if runtime is not None:
        runtime.task = task
        runtime._cb_info(t("det.info_updated", name=task.name))
    return {"task": task_summary(task)}


def _parse_role(raw: object, current: RoleConfig) -> RoleConfig:
    if not isinstance(raw, dict):
        raise ApiError(400, "each role must be an object")
    cli = str(raw.get("cli") or current.cli).strip()
    if cli not in KNOWN_CLIS:
        raise ApiError(400, f"unknown cli: {cli}")
    return RoleConfig(cli=cli, model=str(raw.get("model") or "").strip(),
                      effort=str(raw.get("effort") or "").strip())


def update_roles(service: "ServerService", task_id: str, payload: dict) -> dict:
    """Change the agents (cli/model/effort per role) of a task.

    ``profile`` alone copies that profile's roles; with explicit ``roles``
    the profile name is kept only when the roles match it (TUI rule).
    """
    task = _load_or_404(task_id)
    if _is_running(service, task.id):
        raise ApiError(409, t("api.err.running"))
    profile_name = str(payload.get("profile") or "").strip()
    profile_obj = profiles_module.find(profile_name) if profile_name else None
    if profile_name and profile_obj is None:
        raise ApiError(400, f"unknown profile: {profile_name}")
    roles = payload.get("roles")
    if roles is None and profile_obj is None:
        raise ApiError(400, "roles or profile is required")
    if roles is not None:
        if not isinstance(roles, dict):
            raise ApiError(400, "roles must be an object")
        for role in ROLE_NAMES:
            if role in roles:
                setattr(task, role, _parse_role(roles[role], task.role(role)))
    else:
        for role in ROLE_NAMES:
            source = profile_obj.role(role)
            setattr(task, role, RoleConfig(source.cli, source.model, source.effort))
    matches = profile_obj is not None and all(
        (task.role(role).cli, task.role(role).model, task.role(role).effort)
        == (profile_obj.role(role).cli, profile_obj.role(role).model, profile_obj.role(role).effort)
        for role in ROLE_NAMES
    )
    task.profile = profile_obj.name if matches else ""
    models.save(task)
    runtime = _runtime_of(service, task.id)
    if runtime is not None:
        runtime.task = task
    return {"task": task_detail(task)}


def resume_task(service: "ServerService", task_id: str) -> dict:
    """Resume a FAILED task reusing the artifacts on disk."""
    task = _load_or_404(task_id)
    if task.state is not TaskState.FAILED:
        raise ApiError(409, "task is not in FAILED state")
    _runtime_start(service, task, lambda orch: orch.run_automode_resume(), "API resume")
    return {"ok": True}


async def restart_task(service: "ServerService", task_id: str) -> dict:
    """Reset the task to DRAFT and start automode again."""
    task = _load_or_404(task_id)
    await _cancel_and_wait(service, task)
    task = _load_or_404(task_id)
    models.reset_to_draft(task)
    _runtime_start(service, task, lambda orch: orch.run_automode(), "API restart")
    return {"ok": True}


def extend_task(
    service: "ServerService",
    task_id: str,
    request: str,
    attachments: list[tuple[str, bytes]] | None = None,
) -> dict:
    """Start a new cycle on an existing task with a fresh request (TUI "ask
    for more"): it plans and, unless the task asks for plan confirmation,
    implements and reviews. Attachments are saved to the task media and
    referenced in the request."""
    task = _load_or_404(task_id)
    request = (request or "").strip()
    if not request:
        raise ApiError(400, "request is required")
    _ensure_idle(service, task)
    references = []
    for name, data in attachments or []:
        saved = media.save_attachment(task.id, name, data)
        if saved is not None:
            references.append(
                f"- media/{saved.name}" if saved.suffix.lower() in media.IMAGE_SUFFIXES else f"- {saved}"
            )
    if references:
        request += "\n\n" + "\n".join(references)
    task.start_new_cycle(request)
    models.save(task)
    runner, label, plan_then_ask = _pipeline_runner(task)
    _runtime_start(service, task, runner, label, plan_then_ask)
    return {"ok": True}


def pause_task(service: "ServerService", task_id: str) -> dict:
    """Pause a running task (cancels the worker)."""
    task = _load_or_404(task_id)
    app = service.app
    if app is None:
        raise ApiError(503, "app unavailable")
    runtime = app.runtimes.get(task.id)
    if runtime is None or not runtime.running:
        raise ApiError(409, "not running")
    runtime.cancel()
    return {"ok": True, "state": "paused"}


async def discard_task(service: "ServerService", task_id: str) -> dict:
    """Mark the task as DISCARDED (cancel any running pipeline first).

    Waits for the cancelled worker to stop: its cancellation handler saves
    the task as PAUSED, which would otherwise overwrite DISCARDED.
    """
    task = _load_or_404(task_id)
    await _cancel_and_wait(service, task)
    task = _load_or_404(task_id)
    task.state = TaskState.DISCARDED
    models.save(task)
    runtime = _runtime_of(service, task.id)
    if runtime is not None and not runtime.running:
        runtime.task = task
        runtime._cb_info(t("det.marked.discarded"))
    return {"ok": True, "state": "discarded"}


def mark_done(service: "ServerService", task_id: str) -> dict:
    """Force-complete the task without running the rest of the pipeline."""
    task = _load_or_404(task_id)
    _ensure_idle(service, task)
    task.state = TaskState.DONE
    models.save(task)
    runtime = _runtime_of(service, task.id)
    if runtime is not None:
        runtime.task = task
        runtime._cb_info(t("det.marked.done"))
    return {"ok": True, "state": "done"}


async def synthesize_speech(
    service: "ServerService", text: str, fmt: str = "wav"
) -> tuple[bytes, str]:
    """Synthesize ``text`` with the configured TTS provider.

    Returns ``(audio_bytes, mime)``. Raises ``ApiError`` on validation
    errors (400), missing configuration (503), provider failures (502) and
    unavailable OGG conversion (503). Explicit on-demand synthesis: it only
    requires a configured TTS/STT key, not ``telegram.tts_enabled`` (that
    flag gates the automatic voice replies of the Telegram bot).
    """
    from .. import config as config_module

    text = (text or "").strip()
    if not text:
        raise ApiError(400, "text is required")
    if fmt not in ("wav", "ogg"):
        raise ApiError(400, "format must be wav or ogg")
    telegram_cfg = config_module.load().telegram
    key = telegram_cfg.resolve_tts_key()
    if not key:
        raise ApiError(503, "tts not configured")
    reasons: list[str] = []
    audio = await asyncio.to_thread(
        tts.synthesize,
        url=telegram_cfg.tts_url,
        api_key=key,
        model=telegram_cfg.tts_model,
        voice=telegram_cfg.tts_voice,
        text=text,
        on_error=reasons.append,
    )
    if not audio:
        detail = reasons[0] if reasons else "unknown"
        service._log(f"tts synthesis failed: {detail}")
        raise ApiError(502, f"tts provider error: {detail}")
    if fmt == "wav":
        return audio, "audio/wav"
    voice = await asyncio.to_thread(tts.to_ogg, audio)
    if voice is None:
        raise ApiError(503, "ogg conversion unavailable (ffmpeg missing or failed)")
    return voice, "audio/ogg"
