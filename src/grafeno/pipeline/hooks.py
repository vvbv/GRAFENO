"""Hooks de completado: comando shell o URL (webhook) disparado al terminar etapas.

Hay un hook global (config) y uno opcional por tarea que lo sustituye
(``override``) o se suma a él (``both``). Los hooks shell reciben el contexto de
la ejecución por variables de entorno ``GRAFENO_*``; un hook configurado como
URL http(s) dispara un GET de mejor esfuerzo con un mensaje plano que resume la
tarea. En ambos casos los hooks nunca interrumpen el pipeline: cualquier fallo
solo se registra en el log.
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
import urllib.request
from typing import Callable

from .. import config as config_module
from ..drivers.base import EventKind, RunEvent
from ..i18n import t
from ..models import Task

HOOK_STAGES = ("plan", "implement", "review", "fix", "final", "tests")
HOOK_TIMEOUT_S = 120  # un hook colgado no debe bloquear el pipeline
WEBHOOK_TIMEOUT_S = 30  # las notificaciones deben ser rápidas
MESSAGE_PLACEHOLDER = "{message}"  # marca en la URL donde va el texto


def parse_stages(value: str) -> list[str]:
    """Normaliza una lista de etapas separadas por comas (orden HOOK_STAGES)."""
    chosen = {part.strip() for part in value.split(",") if part.strip()}
    return [stage for stage in HOOK_STAGES if stage in chosen]


def format_stages(stages: list[str]) -> str:
    """Serializa etapas al formato persistido: separadas por comas."""
    return ",".join(stage for stage in HOOK_STAGES if stage in set(stages))


def is_url(command: str) -> bool:
    """True si el hook es una URL http(s) (webhook) y no un comando shell."""
    return command.startswith(("http://", "https://"))


def build_message(task: Task, stage: str, outcome: str) -> str:
    """Texto plano para webhooks: nombre, etapa, resultado y estado de la tarea."""
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
    """Inserta el mensaje en la URL: placeholder {message} o parámetro `text`."""
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
    """GET de mejor esfuerzo en un hilo; devuelve el código de estado HTTP."""

    def _fetch() -> int:
        request = urllib.request.Request(url, headers={"User-Agent": "grafeno"})
        with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT_S) as response:
            response.read(512)  # drena algo de cuerpo para conexiones keep-alive
            return response.status

    return await asyncio.to_thread(_fetch)


def resolve_commands(task: Task, stage: str) -> list[str]:
    """Comandos de hook a ejecutar para una etapa, en orden (global, tarea)."""
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
    """Entorno del subproceso: el del proceso más el contexto GRAFENO_*."""
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
    """Ejecuta en orden los hooks configurados para la etapa (mejor esfuerzo)."""
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
    """Ejecuta un hook comando shell con timeout (contenido del bucle original)."""
    on_info(t("hook.run", stage=t(f"phase.{stage}"), command=command))
    try:
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=task.workdir,
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
    """Envía el mensaje de contexto a una URL (webhook) con timeout implícito."""
    safe_url = urllib.parse.urlunsplit(
        urllib.parse.urlsplit(url)._replace(query="")
    )
    on_info(t("hook.webhook.run", stage=t(f"phase.{stage}"), url=safe_url))
    try:
        status = await _send_webhook(build_webhook_url(url, build_message(task, stage, outcome)))
    except Exception as exc:  # noqa: BLE001 — los hooks nunca rompen el pipeline
        on_info(t("hook.webhook.failed", error=exc))
        return
    on_info(t("hook.webhook.done", status=status))
