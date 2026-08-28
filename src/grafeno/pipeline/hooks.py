"""Completion hooks: shell command or URL (webhook) fired when stages finish.

There is a global hook (config) and an optional per-task one that either
replaces it (``override``) or runs alongside (``both``). Shell hooks receive
the run context through ``GRAFENO_*`` environment variables; a hook set as
an http(s) URL fires a best-effort GET with a plain-text message summarising
the task. In both cases hooks never break the pipeline: any failure is only
recorded in the log.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
from typing import Callable

from .. import config as config_module
from .. import remote
from ..drivers.base import EventKind, RunEvent
from ..i18n import t
from ..models import Task

HOOK_STAGES = ("plan", "implement", "review", "fix", "final", "tests")
HOOK_TIMEOUT_S = 120  # a hung hook must not block the pipeline
WEBHOOK_TIMEOUT_S = 30  # notifications should be fast
MESSAGE_PLACEHOLDER = "{message}"  # marker in the URL where the text goes


def parse_stages(value: str) -> list[str]:
    """Normalize a comma-separated stage list (HOOK_STAGES order)."""
    chosen = {part.strip() for part in value.split(",") if part.strip()}
    return [stage for stage in HOOK_STAGES if stage in chosen]


def format_stages(stages: list[str]) -> str:
    """Serialize stages to the persisted format: comma-separated."""
    return ",".join(stage for stage in HOOK_STAGES if stage in set(stages))


def is_url(command: str) -> bool:
    """True if the hook is an http(s) URL (webhook) rather than a shell command."""
    return command.startswith(("http://", "https://"))


def build_message(task: Task, stage: str, outcome: str) -> str:
    """Plain text for webhooks: name, stage, result and task state."""
    return t(
        "hook.message",
        name=task.name,
        stage=t(f"phase.{stage}"),
        outcome=t(f"hook.outcome.{outcome}"),
        state=task.state.value,
        cycle=task.cycle,
        iteration=task.iteration,
    )


def build_webhook_url(url: str, message: str) -> str:
    """Insert the message into the URL: {message} placeholder or `text` parameter."""
    if MESSAGE_PLACEHOLDER in url:
        return url.replace(MESSAGE_PLACEHOLDER, urllib.parse.quote(message, safe=""))
    parts = urllib.parse.urlsplit(url)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != "text"
    ]
    query.append(("text", message))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


async def _send_webhook(url: str) -> int:
    """Best-effort GET in a thread; returns the HTTP status code."""

    def _fetch() -> int:
        request = urllib.request.Request(url, headers={"User-Agent": "grafeno"})
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as response:
            response.read(512)  # drain some body for keep-alive connections
            return response.status

    return await asyncio.to_thread(_fetch)


def resolve_commands(task: Task, stage: str) -> list[str]:
    """Hook commands to run for a stage, in order (global, task)."""
    commands: list[str] = []
    global_hook = config_module.load().hook
    task_has_hook = bool(task.hook_command.strip())
    use_global = not task_has_hook or task.hook_mode == "both"
    if use_global and stage in parse_stages(global_hook.stages):
        command = global_hook.command.strip()
        if command:
            commands.append(command)
    if task_has_hook and stage in parse_stages(task.hook_stages):
        commands.append(task.hook_command.strip())
    return commands


def _hook_env(task: Task, stage: str, outcome: str) -> dict[str, str]:
    """Subprocess environment: the process one plus the GRAFENO_* context."""
    env = dict(os.environ)
    env.update(
        GRAFENO_TASK_ID=task.id,
        GRAFENO_TASK_NAME=task.name,
        GRAFENO_TASK_WORKDIR=task.workdir,
        GRAFENO_PHASE=stage,
        GRAFENO_OUTCOME=outcome,  # "ok" | "failed"
        GRAFENO_STATE=task.state.value,
        GRAFENO_ITERATION=str(task.iteration),
        GRAFENO_CYCLE=str(task.cycle),
    )
    return env


async def run_stage_hooks(
    task: Task,
    stage: str,
    outcome: str,
    *,
    on_event: Callable[[str, RunEvent], None],
    on_info: Callable[[str], None],
) -> None:
    """Run, in order, the hooks configured for the stage (best effort)."""
    for command in resolve_commands(task, stage):
        if is_url(command):
            await _run_webhook_hook(command, task, stage, outcome, on_info=on_info)
        else:
            await _run_shell_hook(
                command, task, stage, outcome, on_event=on_event, on_info=on_info
            )


async def _run_shell_hook(
    command: str,
    task: Task,
    stage: str,
    outcome: str,
    *,
    on_event: Callable[[str, RunEvent], None],
    on_info: Callable[[str], None],
) -> None:
    """Run a shell-command hook with a timeout (original loop body)."""
    on_info(t("hook.run", stage=t(f"phase.{stage}"), command=command))
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=remote.effective_workdir(task.remote, task.workdir),
            env=_hook_env(task, stage, outcome),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        on_info(t("hook.exec_error", error=exc))
        return
    assert process.stdout is not None
    try:
        output, _ = await asyncio.wait_for(
            process.communicate(), timeout=HOOK_TIMEOUT_S
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        on_info(t("hook.timeout", seconds=HOOK_TIMEOUT_S))
        return
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            on_event("hook", RunEvent(EventKind.INFO, line))
    if process.returncode == 0:
        on_info(t("hook.done"))
    else:
        on_info(t("hook.failed", code=process.returncode))


async def _run_webhook_hook(
    url: str,
    task: Task,
    stage: str,
    outcome: str,
    *,
    on_info: Callable[[str], None],
) -> None:
    """Send the context message to a URL (webhook) with implicit timeout."""
    safe_url = urllib.parse.urlunsplit(
        urllib.parse.urlsplit(url)._replace(query="")
    )
    on_info(t("hook.webhook.run", stage=t(f"phase.{stage}"), url=safe_url))
    try:
        status = await _send_webhook(build_webhook_url(url, build_message(task, stage, outcome)))
    except Exception as exc:  # noqa: BLE001 - hooks never break the pipeline
        on_info(t("hook.webhook.failed", error=exc))
        return
    on_info(t("hook.webhook.done", status=status))
