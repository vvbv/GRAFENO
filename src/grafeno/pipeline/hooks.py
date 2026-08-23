"""Hooks de completado: comando shell del usuario disparado al terminar etapas.

Hay un hook global (config) y uno opcional por tarea que lo sustituye
(``override``) o se suma a él (``both``). Los hooks reciben el contexto de la
ejecución por variables de entorno ``GRAFENO_*`` y nunca interrumpen el
pipeline: cualquier fallo solo se registra en el log.
"""

from __future__ import annotations

import asyncio
import os
from typing import Callable

from .. import config as config_module
from ..drivers.base import EventKind, RunEvent
from ..i18n import t
from ..models import Task

HOOK_STAGES = ("plan", "implement", "review", "fix", "final", "tests")
HOOK_TIMEOUT_S = 120  # un hook colgado no debe bloquear el pipeline


def parse_stages(value: str) -> list[str]:
    """Normaliza una lista de etapas separadas por comas (orden HOOK_STAGES)."""
    chosen = {part.strip() for part in value.split(",") if part.strip()}
    return [stage for stage in HOOK_STAGES if stage in chosen]


def format_stages(stages: list[str]) -> str:
    """Serializa etapas al formato persistido: separadas por comas."""
    return ",".join(stage for stage in HOOK_STAGES if stage in set(stages))


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
            continue
        assert process.stdout is not None
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=HOOK_TIMEOUT_S
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            on_info(t("hook.timeout", seconds=HOOK_TIMEOUT_S))
            continue
        for line in output.decode("utf-8", errors="replace").splitlines():
            if line.strip():
                on_event("hook", RunEvent(EventKind.INFO, line))
        if process.returncode == 0:
            on_info(t("hook.done"))
        else:
            on_info(t("hook.failed", code=process.returncode))
