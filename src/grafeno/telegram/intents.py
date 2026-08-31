"""Intent parsing for the Telegram bot.

The user's text (typed or transcribed from a voice note) is interpreted by
one of the already-configured agent CLIs: a one-shot prompt asks for a
strict JSON payload describing the action to perform (create task(s), list,
status, send files, ask about a task, help). No extra LLM SDK or API key is
needed: it reuses the driver layer like any pipeline phase.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..drivers.base import CLIDriver, RunRequest
from ..i18n import LANGUAGES
from ..models import Task, state_label

ACTIONS = (
    "create_tasks",  # create one or more tasks (params in ``tasks``)
    "list_tasks",    # summary of the existing tasks
    "task_status",   # status of one task (``task_ref``)
    "send_files",    # send the task .md artifacts (``task_ref``)
    "ask",           # answer a question about a task (``task_ref`` + ``question``)
    "help",          # usage help
    "unknown",       # could not understand
)

MAX_TASKS_PER_INTENT = 10
PARSER_TIMEOUT = 120.0  # seconds; a hung parser CLI must not wedge the bot
_SUMMARY_LIMIT = 30


@dataclass
class TaskSpec:
    """Task proposed by the parser (parameters confirmed by the user)."""

    name: str
    description: str = ""
    workdir: str = ""
    test_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "workdir": self.workdir,
            "test_command": self.test_command,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        return cls(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            workdir=str(data.get("workdir", "")),
            test_command=str(data.get("test_command", "")),
        )


@dataclass
class Intent:
    action: str = "unknown"
    tasks: list[TaskSpec] = field(default_factory=list)
    task_ref: str = ""   # id or name fragment the user refers to
    question: str = ""   # question for the "ask" action
    error: str = ""      # parser CLI infrastructure failure (not "unknown")
    lang: str = ""       # ISO code of the user's language ("" = unknown)


def tasks_summary(tasks: list[Task], *, limit: int = _SUMMARY_LIMIT) -> str:
    """Compact ``- id | name | state | workdir`` listing used as parser context.

    The workdir column lets the parser route new tasks to the right project
    and disambiguate task references; remote tasks show their SSH spec.
    """
    lines = [
        f"- {task.id} | {task.name} | {state_label(task.state)} | {_task_dir(task)}"
        for task in tasks[:limit]
    ]
    return "\n".join(lines)


def _task_dir(task: Task) -> str:
    """Directory shown in the parser context: SSH spec for remote tasks."""
    return task.remote if task.is_remote else task.workdir


def build_parser_prompt(user_text: str, summary: str, default_workdir: str) -> str:
    """One-shot prompt: interpret the user message and answer with strict JSON."""
    return f"""Eres el interpretador de mensajes de GRAFENO, un orquestador de tareas de
programación. El usuario escribe o dicta por voz mensajes para crear tareas o
consultar las existentes.

Tareas existentes (id | nombre | estado | directorio):
{summary or "(ninguna)"}

Directorio de trabajo por defecto para tareas nuevas: {default_workdir or "."}

Mensaje del usuario:
\"\"\"
{user_text}
\"\"\"

Responde SOLO con un objeto JSON (sin texto alrededor, sin Markdown) con esta forma:
{{
  "action": "create_tasks" | "list_tasks" | "task_status" | "send_files" | "ask" | "help" | "unknown",
  "tasks": [{{"name": "...", "description": "...", "workdir": "...", "test_command": "..."}}],
  "task_ref": "id o fragmento del nombre de la tarea (para task_status, send_files, ask)",
  "question": "la pregunta concreta del usuario (solo para ask)",
  "lang": "código ISO 639-1 del idioma del mensaje del usuario (es, en, ...)"
}}

Reglas:
- "lang": SIEMPRE el idioma en que el usuario escribió o dictó el mensaje.
- "create_tasks": una entrada por cada tarea que pida el mensaje; name corto y
  descriptivo; description detallada incluyendo TODO lo que pida el usuario.
  Para "workdir": si el mensaje se refiere a un proyecto con tareas
  existentes (por nombre de proyecto o de directorio), usa EXACTAMENTE el
  directorio de esa tarea del listado (sin inventar rutas); si el usuario
  indica una ruta explícita, úsala tal cual; si no se puede determinar el
  proyecto, déjalo vacío (se usará el directorio por defecto). Las rutas
  "user@host:..." son proyectos remotos: no las uses para tareas nuevas.
  test_command solo si lo indica.
- Si el mensaje pide varias tareas, inclúyelas todas en "tasks".
- "list_tasks": el usuario quiere un resumen de sus tareas.
- "task_status": pregunta por el estado de una tarea concreta.
- "send_files": el usuario quiere que le envíes los archivos .md resultantes de
  una tarea (plan, revisiones, informe final).
- "ask": cualquier otra pregunta sobre una tarea concreta.
- "help": pide ayuda o no queda claro qué hacer.
- "unknown": no se puede interpretar.
- No inventes tareas: crea solo lo que el mensaje pida explícitamente.
- Usa el listado de tareas (id | nombre | estado | directorio) para resolver
  "task_ref" de forma inequívoca: devuelve el id exacto de la tarea que mejor
  encaje con lo que pide el usuario; el directorio ayuda a distinguir tareas
  con nombres parecidos en proyectos distintos.
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract the first balanced ``{...}`` block from the CLI output."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    payload = json.loads(text[start : index + 1])
                except ValueError:
                    return None
                return payload if isinstance(payload, dict) else None
    return None


def _parse_lang(value: Any) -> str:
    """ISO code of the user's language; only catalog languages are kept."""
    lang = str(value or "").strip().lower()[:2]
    return lang if lang in LANGUAGES else ""


def parse_intent_payload(text: str) -> Intent:
    """Tolerant parse of the parser CLI output into an Intent."""
    payload = _extract_json(text)
    if payload is None:
        return Intent(action="unknown")
    action = str(payload.get("action", "unknown"))
    if action not in ACTIONS:
        action = "unknown"
    specs: list[TaskSpec] = []
    raw_tasks = payload.get("tasks")
    if isinstance(raw_tasks, list):
        for item in raw_tasks[:MAX_TASKS_PER_INTENT]:
            if not isinstance(item, dict):
                continue
            spec = TaskSpec.from_dict(item)
            if spec.name.strip():
                specs.append(spec)
    intent = Intent(
        action=action,
        tasks=specs,
        task_ref=str(payload.get("task_ref", "") or "").strip(),
        question=str(payload.get("question", "") or "").strip(),
        lang=_parse_lang(payload.get("lang")),
    )
    if intent.action == "create_tasks" and not intent.tasks:
        intent.action = "unknown"  # nothing valid to create
    if intent.action in ("task_status", "send_files", "ask") and not intent.task_ref:
        intent.action = "help"  # no target task: better to explain usage
    return intent


async def parse_intent(
    driver: CLIDriver,
    model: str,
    user_text: str,
    summary: str,
    workdir: Path,
    *,
    default_workdir: str = "",
    timeout: float = PARSER_TIMEOUT,
) -> Intent:
    """Run the parser CLI one-shot and interpret its JSON answer.

    Infrastructure failures (timeout, crash, non-zero exit) are reported in
    ``Intent.error`` so the caller can tell them apart from a genuine
    "unknown" intent and surface them to the user instead of staying silent.
    """
    prompt = build_parser_prompt(user_text, summary, default_workdir)
    request = RunRequest(
        prompt=prompt,
        model=model,
        workdir=workdir,
        title="grafeno:telegram:intent",
    )
    try:
        result = await asyncio.wait_for(driver.run(request), timeout=timeout)
    except TimeoutError:
        return Intent(action="unknown", error=f"timeout after {timeout:.0f}s")
    except Exception as exc:  # noqa: BLE001 - the bot never propagates CLI errors
        return Intent(action="unknown", error=str(exc)[:300])
    if not result.ok:
        return Intent(action="unknown", error=(result.error or "exit error")[:300])
    if not result.text.strip():
        return Intent(action="unknown", error="empty output")
    return parse_intent_payload(result.text)


def fuzzy_find_task(query: str, tasks: list[Task]) -> Task | None:
    """Best match for a user reference: exact id, id prefix, then name."""
    needle = query.strip().lower()
    if not needle:
        return None
    for task in tasks:
        if task.id.lower() == needle:
            return task
    for task in tasks:
        if task.id.lower().startswith(needle):
            return task
    for task in tasks:
        if task.name.strip().lower() == needle:
            return task
    for task in tasks:
        if needle in task.name.lower():
            return task
    return None


def resolve_workdir(spec_workdir: str, tasks: list[Task], default: str = "") -> str:
    """Canonical workdir for a parsed TaskSpec.

    Empty means "not determined" and falls back to the default directory
    (current behavior). A value matching the directory of an existing task
    (local workdir or remote spec) is normalized to that exact string. Any
    other value is passed through stripped: the service validates that the
    directory exists, as it does today for explicit paths.
    """
    candidate = spec_workdir.strip()
    if not candidate:
        return default.strip() or "."
    lowered = candidate.lower()
    for task in tasks:
        if candidate == task.workdir or lowered == task.workdir.strip().lower():
            return task.workdir
        if task.is_remote and lowered == task.remote.strip().lower():
            return task.remote
    return candidate
