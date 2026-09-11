"""Read/write operations shared by the REST and WebSocket APIs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import media, models, paths, scheduler
from ..models import Task, TaskState, task_state_label
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


def task_summary(task: Task) -> dict:
    """Slim task payload for list endpoints and WS events."""
    return {
        "id": task.id,
        "name": task.name,
        "workdir": task.workdir,
        "remote": task.remote,
        "state": task.state.value,
        "state_label": task_state_label(task),
        "automode": task.automode,
        "origin": task.origin,
        "scheduled_at": task.scheduled_at,
        "parent_id": task.parent_id,
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
    """List tasks, optionally filtered by state."""
    tasks = models.list_all()
    if state:
        try:
            wanted = TaskState(state)
        except ValueError as exc:
            raise ApiError(400, f"unknown state: {state}") from exc
        tasks = [task for task in tasks if task.state is wanted]
    return {"tasks": [task_summary(task) for task in tasks]}


def get_task(service: "ServerService", task_id: str) -> dict:
    """Return the full payload of one task.

    The plan's contract is ``{"task": task_detail(task)}``: the response
    is wrapped under a ``task`` key (symmetric with ``list_tasks`` which
    wraps each item in ``{"tasks": [...]}``). ``task_detail`` itself
    already contains a nested ``task`` key (it returns ``Task.to_dict()``
    plus a few derived fields), so the result intentionally has a
    ``task.task.id`` location for the identifier; this matches the plan.
    """
    try:
        task = models.load(task_id)
    except (FileNotFoundError, KeyError, OSError, ValueError) as exc:
        raise ApiError(404, "task not found") from exc
    return {"task": task_detail(task)}


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
    return {"logs": lines}


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


# ---------------------------------------------------------------------- #
# Write operations
# ---------------------------------------------------------------------- #
def _runtime_start(service: "ServerService", task: Task, runner_factory, label: str) -> None:
    """Start a runner on the task's runtime, raising ApiError on failures."""
    app = service.app
    if app is None:
        raise ApiError(503, "app unavailable")
    runtime = service.app.runtime_for(task)
    if not runtime.start(app, runner_factory, label):
        raise ApiError(409, "task already running")


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


async def create_task(service: "ServerService", payload: dict) -> dict:
    """Create a new task from a JSON payload and return its summary."""
    from .. import config as config_module

    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be an object")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ApiError(400, "name is required")
    workdir = str(payload.get("workdir") or "").strip()
    if not workdir:
        raise ApiError(400, "workdir is required")
    description = str(payload.get("description") or "")
    attachments = _decode_attachments(payload)  # 400 before creating the task
    cfg = config_module.load()
    parent_id = str(payload.get("parent_id") or "").strip()
    automode = _coerce_bool(payload.get("automode"), True)
    if parent_id:
        # Validate the proposed position using the same rule the TUI uses.
        task = models.Task.create(
            name=name,
            description=description,
            workdir=workdir,
            config=cfg,
            automode=automode,
            parent_id=parent_id or None,
        )
        by_id = {item.id: item for item in models.list_all()}
        error = scheduler.rechain_error(task, parent_id, by_id)
        if error:
            raise ApiError(400, error)
    else:
        task = models.Task.create(
            name=name,
            description=description,
            workdir=workdir,
            config=cfg,
            automode=automode,
        )
    scheduled_at = payload.get("scheduled_at")
    if scheduled_at:
        task.scheduled_at = str(scheduled_at)
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
    """Start the full automode pipeline of a task."""
    task = _load_or_404(task_id)
    _runtime_start(service, task, lambda orch: orch.run_automode(), "API start")
    return {"ok": True}


def resume_task(service: "ServerService", task_id: str) -> dict:
    """Resume a FAILED task reusing the artifacts on disk."""
    task = _load_or_404(task_id)
    if task.state is not TaskState.FAILED:
        raise ApiError(409, "task is not in FAILED state")
    _runtime_start(service, task, lambda orch: orch.run_automode_resume(), "API resume")
    return {"ok": True}


def restart_task(service: "ServerService", task_id: str) -> dict:
    """Reset the task to DRAFT and start automode again."""
    task = _load_or_404(task_id)
    models.reset_to_draft(task)
    _runtime_start(service, task, lambda orch: orch.run_automode(), "API restart")
    return {"ok": True}


def extend_task(service: "ServerService", task_id: str, request: str) -> dict:
    """Start a new cycle on an existing task with a fresh request."""
    task = _load_or_404(task_id)
    request = (request or "").strip()
    if not request:
        raise ApiError(400, "request is required")
    task.start_new_cycle(request)
    models.save(task)
    _runtime_start(service, task, lambda orch: orch.run_automode_plan(), "API extend")
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


def discard_task(service: "ServerService", task_id: str) -> dict:
    """Mark the task as DISCARDED (cancel any running pipeline first)."""
    task = _load_or_404(task_id)
    app = service.app
    if app is not None:
        runtime = app.runtimes.get(task.id)
        if runtime is not None and runtime.running:
            runtime.cancel()
    task.state = TaskState.DISCARDED
    models.save(task)
    return {"ok": True, "state": "discarded"}


def mark_done(service: "ServerService", task_id: str) -> dict:
    """Force-complete the task without running the rest of the pipeline."""
    task = _load_or_404(task_id)
    task.state = TaskState.DONE
    models.save(task)
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
