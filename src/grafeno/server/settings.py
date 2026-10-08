"""Catalog, report and settings operations shared by the REST and WS APIs.

Complements :mod:`server.actions` (task operations) with what the TUI
screens other than the task detail expose: the new-task form defaults,
the CLI model catalogue, directory autocompletion, GitHub issues, the
usage reports and the global settings screen.

Secrets of the settings (Telegram bot token, STT/TTS keys and API tokens)
are write-only: reads report whether they are set, never their value.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from .. import config as config_module
from .. import editor as editor_module
from .. import gh as gh_module
from .. import paths
from .. import profiles as profiles_module
from .. import references as references_module
from .. import remotesession
from .. import triggers as triggers_module
from .. import usage
from ..config import DEFAULT_API_HOST, DEFAULT_API_PORT, KNOWN_CLIS, RoleConfig
from ..i18n import LANGUAGES, set_language, set_prompt_language, t
from ..pipeline.hooks import HOOK_STAGES, format_stages, parse_stages
from ..profiles import Profile, roles_summary
from ..references import Reference
from ..triggers import ALL_PHASES, TIMINGS, TRIGGER_STAGES, Trigger
from .actions import ROLE_NAMES, ApiError, _coerce_bool

if TYPE_CHECKING:
    from .service import ServerService

MODELS_TTL_SECONDS = 600   # model catalogue cache lifetime
MAX_DIR_ENTRIES = 200      # directory autocompletion cap


def _role_dict(role: RoleConfig) -> dict:
    return {"cli": role.cli, "model": role.model, "effort": role.effort}


def _profile_dict(profile: Profile) -> dict:
    return {
        "name": profile.name,
        "summary": profile.summary(),
        "roles": {role: _role_dict(profile.role(role)) for role in ROLE_NAMES},
    }


# ---------------------------------------------------------------------- #
# New-task form
# ---------------------------------------------------------------------- #
def form_options(service: "ServerService") -> dict:
    """Defaults and choices of the new-task form (TUI ``NewTaskScreen``)."""
    from ..drivers import available_clis

    cfg = config_module.load()
    return {
        "defaults": {
            "workdir": os.getcwd(),
            "automode": cfg.automode.enabled,
            "confirm_plan": cfg.automode.confirm_plan,
            "create_branch": cfg.automode.create_branch,
            "test_command": cfg.automode.test_command,
            "first_prompt": cfg.first_prompt,
            "final_prompt": cfg.final_prompt,
            "roles": {role: _role_dict(cfg.role(role)) for role in ROLE_NAMES},
            "roles_summary": roles_summary({role: cfg.role(role) for role in ROLE_NAMES}),
        },
        "profiles": [_profile_dict(profile) for profile in profiles_module.load_global()],
        "hook_stages": list(HOOK_STAGES),
        "known_clis": list(KNOWN_CLIS),
        "available_clis": available_clis(),
        "session": {
            "active": remotesession.active(),
            "label": remotesession.label() if remotesession.active() else "",
        },
    }


async def models_catalog(service: "ServerService", refresh: bool = False) -> dict:
    """Models and effort variants per CLI (cached; the CLIs are slow)."""
    from ..drivers import fetch_all_models, fetch_all_variants

    cached = getattr(service, "_models_cache", None)
    if cached and not refresh and time.monotonic() - cached[0] < MODELS_TTL_SECONDS:
        return cached[1]
    models_map = await fetch_all_models(KNOWN_CLIS)
    variants_map = await fetch_all_variants(KNOWN_CLIS)
    result = {"models": models_map, "variants": variants_map}
    service._models_cache = (time.monotonic(), result)
    return result


def list_dirs(service: "ServerService", raw_path: str) -> dict:
    """Subdirectories for the workdir autocompletion (TUI ``DirectoryPicker``).

    ``raw_path`` may be a directory (its children are listed) or a partial
    path (the children of its parent whose name starts with the last part).
    """
    text = (raw_path or "").strip() or os.getcwd()
    path = Path(text).expanduser()
    if text.endswith(("/", os.sep)) or path.is_dir():
        base, prefix = path, ""
    else:
        base, prefix = path.parent, path.name
    entries: list[str] = []
    try:
        for entry in sorted(base.iterdir(), key=lambda item: item.name.lower()):
            if len(entries) >= MAX_DIR_ENTRIES:
                break
            if entry.name.startswith(".") and not prefix.startswith("."):
                continue
            if prefix and not entry.name.lower().startswith(prefix.lower()):
                continue
            if entry.is_dir():
                entries.append(str(entry))
    except OSError:
        entries = []
    return {
        "path": str(base),
        "parent": str(base.parent) if base.parent != base else "",
        "dirs": entries,
        "exists": base.is_dir(),
    }


async def list_issues(service: "ServerService", workdir: str) -> dict:
    """Open GitHub issues of the project (empty without gh access)."""
    path = Path((workdir or "").strip() or ".").expanduser()
    if remotesession.active() or not path.is_dir():
        return {"available": False, "issues": []}
    available = await asyncio.to_thread(gh_module.gh_available, path)
    issues = await asyncio.to_thread(gh_module.list_issues, path) if available else []
    return {
        "available": available,
        "issues": [
            {"number": issue.number, "title": issue.title, "body": issue.body}
            for issue in issues
        ],
    }


# ---------------------------------------------------------------------- #
# Usage reports
# ---------------------------------------------------------------------- #
def _parse_day(value: str, field: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(400, f"{field} must be YYYY-MM-DD") from exc


def usage_report(service: "ServerService", start: str = "", end: str = "", period: str = "") -> dict:
    """Usage aggregated by day, CLI+model and project (TUI reports screen).

    ``period`` (day/week/month, around today) wins over ``from``/``to``;
    a single date means that day; nothing means today.
    """
    today = date.today()
    if period:
        periods = {"day": usage.period_day, "week": usage.period_week, "month": usage.period_month}
        if period not in periods:
            raise ApiError(400, "period must be day, week or month")
        first, last = periods[period](today)
    else:
        first = _parse_day(start, "from")
        last = _parse_day(end, "to")
        if first is None and last is None:
            first = last = today
        first = first or last
        last = last or first
        if first > last:
            first, last = last, first
    records = usage.records_between(usage.load_records(), first, last)
    summary = usage.summarize(records)
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "tokens": {"input": summary.tokens_in, "output": summary.tokens_out},
        "seconds": summary.seconds,
        "tasks": len(summary.task_ids),
        "projects": len(summary.by_project),
        "by_day": [
            {"date": day, "input": values[0], "output": values[1], "seconds": values[2]}
            for day, values in sorted(summary.by_day.items())
        ],
        "by_model": [
            {"label": label, "input": values[0], "output": values[1]}
            for label, values in sorted(
                summary.by_model.items(), key=lambda item: (-(item[1][0] + item[1][1]), item[0])
            )
        ],
        "by_project": [
            {"project": project, "input": values[0], "output": values[1],
             "seconds": values[2], "tasks": values[3]}
            for project, values in sorted(
                summary.by_project.items(), key=lambda item: (-(item[1][0] + item[1][1]), item[0])
            )
        ],
    }


# ---------------------------------------------------------------------- #
# Global settings (TUI ConfigScreen)
# ---------------------------------------------------------------------- #
def get_settings(service: "ServerService") -> dict:
    """Current global settings; secrets are reported as ``*_set`` flags."""
    cfg = config_module.load()
    tg = cfg.telegram
    return {
        "path": str(paths.config_path()),
        "roles": {role: _role_dict(cfg.role(role)) for role in ROLE_NAMES},
        "automode": cfg.automode.to_dict(),
        "auto_update": cfg.auto_update,
        "self_update": cfg.self_update,
        "first_prompt": cfg.first_prompt,
        "final_prompt": cfg.final_prompt,
        "hook": {"command": cfg.hook.command, "stages": parse_stages(cfg.hook.stages)},
        "editor": cfg.editor.to_dict(),
        "editors_available": editor_module.available_editors(),
        "language": cfg.language,
        "prompt_language": cfg.prompt_language,
        "languages": list(LANGUAGES),
        "workspaces": list(cfg.workspaces),
        "references": [ref.to_dict() for ref in references_module.load_global()],
        "triggers": [trigger.to_dict() for trigger in triggers_module.load_global()],
        "profiles": [_profile_dict(profile) for profile in profiles_module.load_global()],
        "telegram": {
            "enabled": tg.enabled,
            "confirm_create": tg.confirm_create,
            "group_all": tg.group_all,
            "bot_token_set": bool(tg.bot_token),
            "allowed_chat_ids": tg.allowed_chat_ids,
            "parser_cli": tg.parser_cli,
            "parser_model": tg.parser_model,
            "default_workdir": tg.default_workdir,
            "stt_url": tg.stt_url,
            "stt_key_set": bool(tg.stt_key),
            "stt_model": tg.stt_model,
            "tts_enabled": tg.tts_enabled,
            "tts_url": tg.tts_url,
            "tts_key_set": bool(tg.tts_key),
            "tts_model": tg.tts_model,
            "tts_voice": tg.tts_voice,
        },
        "api": {
            "enabled": cfg.api.enabled,
            "host": cfg.api.host,
            "port": cfg.api.port,
            "tokens_set": bool(cfg.api.tokens),
        },
        "hook_stages": list(HOOK_STAGES),
        "trigger_stages": list(TRIGGER_STAGES),
        "trigger_timings": list(TIMINGS),
        "known_clis": list(KNOWN_CLIS),
    }


def _apply_secret(section_obj: object, key: str, data: dict) -> None:
    """Write-only secrets: a non-empty value replaces, ``<key>_clear`` empties."""
    if data.get(f"{key}_clear"):
        setattr(section_obj, key, "")
        return
    value = str(data.get(key) or "").strip()
    if value:
        setattr(section_obj, key, value)


def _parse_triggers(raw: object) -> list[Trigger]:
    if not isinstance(raw, list):
        raise ApiError(400, "triggers must be a list")
    result: list[Trigger] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "each trigger must be an object")
        trigger = Trigger.from_dict(item)
        phases = trigger.phases.strip()
        if phases != ALL_PHASES:
            chosen = [part.strip() for part in phases.split(",") if part.strip() in TRIGGER_STAGES]
            trigger.phases = ",".join(chosen) or ALL_PHASES
        if trigger.name.strip() and trigger.description.strip():
            result.append(trigger)
    return result


def _parse_profiles(raw: object) -> list[Profile]:
    if not isinstance(raw, list):
        raise ApiError(400, "profiles must be a list")
    result: list[Profile] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError(400, "each profile must be an object")
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        profile = Profile(name=name)
        roles = item.get("roles") or {}
        for role in ROLE_NAMES:
            values = roles.get(role) if isinstance(roles, dict) else None
            values = values if isinstance(values, dict) else {}
            cli = str(values.get("cli") or "opencode")
            if cli not in KNOWN_CLIS:
                raise ApiError(400, f"unknown cli: {cli}")
            setattr(profile, role, RoleConfig(
                cli=cli,
                model=str(values.get("model") or ""),
                effort=str(values.get("effort") or ""),
            ))
        result.append(profile)
    return result


def update_settings(service: "ServerService", payload: dict) -> dict:
    """Save the global settings (only the sections present in ``payload``).

    Starts from the saved config, so fields the web panel does not manage
    (theme...) are kept. Language changes apply at once, like the TUI.
    Changes to ``[telegram]``/``[api]`` apply on the next TUI start.
    """
    if not isinstance(payload, dict):
        raise ApiError(400, "payload must be an object")
    cfg = config_module.load()
    roles = payload.get("roles")
    if isinstance(roles, dict):
        for role in ROLE_NAMES:
            values = roles.get(role)
            if not isinstance(values, dict):
                continue
            cli = str(values.get("cli") or cfg.role(role).cli)
            if cli not in KNOWN_CLIS:
                raise ApiError(400, f"unknown cli: {cli}")
            target = cfg.role(role)
            target.cli = cli
            target.model = str(values.get("model") or "").strip()
            target.effort = str(values.get("effort") or "").strip()
    auto = payload.get("automode")
    if isinstance(auto, dict):
        try:
            max_iter = int(auto.get("max_iterations", cfg.automode.max_iterations))
        except (TypeError, ValueError) as exc:
            raise ApiError(400, t("cfg.error.max_iter_int")) from exc
        if max_iter < 1:
            raise ApiError(400, t("cfg.error.max_iter_min"))
        cfg.automode.max_iterations = max_iter
        cfg.automode.enabled = _coerce_bool(auto.get("enabled"), cfg.automode.enabled)
        cfg.automode.create_branch = _coerce_bool(auto.get("create_branch"), cfg.automode.create_branch)
        cfg.automode.confirm_plan = _coerce_bool(auto.get("confirm_plan"), cfg.automode.confirm_plan)
        if "test_command" in auto:
            cfg.automode.test_command = str(auto.get("test_command") or "").strip()
    for key in ("auto_update", "self_update"):
        if key in payload:
            setattr(cfg, key, _coerce_bool(payload[key], getattr(cfg, key)))
    for key in ("first_prompt", "final_prompt"):
        if key in payload:
            setattr(cfg, key, str(payload.get(key) or "").strip())
    hook = payload.get("hook")
    if isinstance(hook, dict):
        cfg.hook.command = str(hook.get("command") or "").strip()
        stages = hook.get("stages") or []
        cfg.hook.stages = format_stages([str(item) for item in stages]) if isinstance(stages, list) else ""
    editor = payload.get("editor")
    if isinstance(editor, dict):
        cfg.editor.enabled = _coerce_bool(editor.get("enabled"), cfg.editor.enabled)
        cfg.editor.editor = str(editor.get("editor") or "").strip()
        mode = str(editor.get("mode") or "window")
        cfg.editor.mode = mode if mode in ("window", "split", "none") else "window"
        side = str(editor.get("side") or "left")
        cfg.editor.side = side if side in ("left", "right") else "left"
    if "language" in payload:
        language = str(payload.get("language") or "")
        cfg.language = language if language in LANGUAGES else cfg.language
    if "prompt_language" in payload:
        prompt_language = str(payload.get("prompt_language") or "")
        cfg.prompt_language = prompt_language if prompt_language in LANGUAGES else ""
    if "workspaces" in payload:
        raw = payload.get("workspaces") or []
        if isinstance(raw, str):
            raw = raw.split(",")
        cfg.workspaces = [str(item).strip() for item in raw if str(item).strip()]
    tg_data = payload.get("telegram")
    if isinstance(tg_data, dict):
        tg = cfg.telegram
        for key in ("enabled", "confirm_create", "group_all", "tts_enabled"):
            if key in tg_data:
                setattr(tg, key, _coerce_bool(tg_data[key], getattr(tg, key)))
        for key in ("allowed_chat_ids", "parser_model", "default_workdir", "stt_url",
                    "stt_model", "tts_url", "tts_model", "tts_voice"):
            if key in tg_data:
                setattr(tg, key, str(tg_data.get(key) or "").strip())
        if "parser_cli" in tg_data:
            parser_cli = str(tg_data.get("parser_cli") or "")
            tg.parser_cli = parser_cli if parser_cli in KNOWN_CLIS else ""
        for key in ("bot_token", "stt_key", "tts_key"):
            _apply_secret(tg, key, tg_data)
    api_data = payload.get("api")
    if isinstance(api_data, dict):
        api = cfg.api
        api.enabled = _coerce_bool(api_data.get("enabled"), api.enabled)
        api.host = str(api_data.get("host") or "").strip() or DEFAULT_API_HOST
        try:
            port = int(api_data.get("port") or DEFAULT_API_PORT)
        except (TypeError, ValueError) as exc:
            raise ApiError(400, t("cfg.error.api_port")) from exc
        if not 1 <= port <= 65535:
            raise ApiError(400, t("cfg.error.api_port"))
        api.port = port
        _apply_secret(api, "tokens", api_data)
    references = triggers = profiles = None
    if "references" in payload:
        raw = payload.get("references")
        if not isinstance(raw, list):
            raise ApiError(400, "references must be a list")
        references = [
            ref for ref in (Reference.from_dict(item) for item in raw if isinstance(item, dict))
            if ref.name.strip() and ref.path.strip()
        ]
    if "triggers" in payload:
        triggers = _parse_triggers(payload.get("triggers"))
    if "profiles" in payload:
        profiles = _parse_profiles(payload.get("profiles"))
    config_module.save(cfg)
    if references is not None:
        references_module.save_global(references)
    if triggers is not None:
        triggers_module.save_global(triggers)
    if profiles is not None:
        profiles_module.save_global(profiles)
    set_language(cfg.language)
    set_prompt_language(cfg.prompt_language)
    return get_settings(service)
