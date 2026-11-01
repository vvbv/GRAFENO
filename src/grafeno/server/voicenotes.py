"""Voice note interpretation for API clients: transcribe and propose tasks, never create them."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config as config_module
from .. import media, models, remotesession
from .. import workspaces as workspaces_module
from ..config import TelegramConfig
from ..drivers import get_driver
from ..drivers.base import CLIDriver
from ..models import Task
from ..telegram import intents, stt
from .actions import ApiError, _norm_dir, _workspace_roots

if TYPE_CHECKING:
    from .service import ServerService


def _audio_suffix(name: str) -> str:
    """Validated audio suffix of the ``?name=`` value (the provider infers the format from it)."""
    if not name.strip():
        raise ApiError(400, "name is required", code="audio_name_required")
    if not media.is_audio_name(name):
        raise ApiError(415, "name must have an audio suffix", code="unsupported_audio")
    return Path(name).suffix.lower()


def _parser_driver(cli: str) -> CLIDriver | None:
    """Driver of the parser CLI when it is supported and installed."""
    try:
        driver = get_driver(cli)
    except (KeyError, NotImplementedError):
        return None
    return driver if driver.is_available() else None


def _run_workdir(telegram: TelegramConfig) -> Path:
    """cwd of the parser one-shot (same choice as the bot; must exist)."""
    candidate = Path(telegram.default_workdir or os.getcwd())
    return candidate if candidate.is_dir() else Path(".")


def _discovered_projects() -> list[str]:
    """Folders of the working directories, the same roots as ``GET /api/v1/projects``."""
    try:
        existing = [Path(root.path) for root in _workspace_roots() if root.exists]
        return [str(path) for path in workspaces_module.discover(existing)]
    except Exception:  # noqa: BLE001 - workspaces discovery is best effort
        return []


def _proposal_workdir(raw: str, tasks: list[Task], default: str, discovered: list[str]) -> str:
    """Canonical local project of a proposal; empty when it cannot be determined.

    Remote specs, relative paths and folders that do not exist are dropped: the
    parser output is not trusted to name a usable directory.
    """
    candidate = raw.strip() or default.strip()
    if not candidate:
        return ""
    # _norm_dir never raises (Path.expanduser() does on an unknown ~user)
    normalized = _norm_dir(intents.resolve_workdir(candidate, tasks, "", discovered))
    path = Path(normalized)
    return normalized if path.is_absolute() and path.is_dir() else ""


async def _transcribe(
    service: "ServerService", telegram: TelegramConfig, key: str, data: bytes, suffix: str
) -> str:
    """Transcription of the note; ``stt_failed`` when the provider gives no text."""
    reasons: list[str] = []
    text = await asyncio.to_thread(
        stt.transcribe,
        url=telegram.stt_url,
        api_key=key,
        model=telegram.stt_model,
        data=data,
        filename=f"voice{suffix}",
        on_error=reasons.append,
    )
    if not text:
        reason = reasons[0] if reasons else "unknown"
        service._log(f"voice note transcription failed: {reason}")
        raise ApiError(502, f"transcription failed: {reason}", code="stt_failed")
    return text


async def interpret_voice_note(service: "ServerService", name: str, data: bytes) -> dict | tuple[int, dict]:
    """Transcribe a voice note and propose tasks with the Telegram bot settings.

    Nothing is stored and no task is created. An interpretation failure also
    returns the transcription, so the client does not lose a paid result.
    """
    if remotesession.active():
        raise ApiError(409, "voice notes are not available in remote session mode", code="remote")
    suffix = _audio_suffix(name)
    if not data:
        raise ApiError(400, "empty body", code="empty_audio")
    cfg = config_module.load()
    telegram = cfg.telegram
    key = telegram.resolve_stt_key()
    if not (telegram.stt_url.strip() and telegram.stt_model.strip() and key):
        raise ApiError(503, "stt not configured", code="stt_not_configured")
    parser = telegram.parser_role(cfg.planner)
    driver = _parser_driver(parser.cli)
    if driver is None:  # checked first: do not spend STT on a request that cannot finish
        raise ApiError(502, f"parser cli not available: {parser.cli or '-'}", code="interpret_failed")
    transcription = await _transcribe(service, telegram, key, data, suffix)
    tasks = models.list_all()
    discovered = _discovered_projects()
    proposal = await intents.propose_voice_tasks(
        driver,
        parser.model,
        transcription,
        intents.tasks_summary(tasks),
        _run_workdir(telegram),
        projects=intents.projects_summary(tasks, discovered),
        timeout=intents.parser_timeout_for(transcription, float(telegram.parser_timeout)),
        retries=intents.PARSER_RETRIES,
        retry_delay=intents.PARSER_RETRY_DELAY,
        effort=parser.effort,
    )
    if proposal.error:
        service._log(f"voice note interpretation failed: {proposal.error}")
        return 502, {
            "error": f"interpretation failed: {proposal.error}",
            "code": "interpret_failed",
            "transcription": transcription,
        }
    service._log(f"voice note interpreted: tasks={len(proposal.tasks)}")  # never the transcription
    return {
        "transcription": transcription,
        "tasks": [
            {
                "title": spec.name.strip(),
                "workdir": _proposal_workdir(spec.workdir, tasks, telegram.default_workdir, discovered),
                "description": spec.description.strip(),
            }
            for spec in proposal.tasks
        ],
    }
