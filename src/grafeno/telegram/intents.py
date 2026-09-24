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
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..drivers.base import CLIDriver, RunRequest
from ..i18n import LANGUAGES, prompt_language, prompt_template, t_lang
from ..models import Task

ACTIONS = (
    "create_tasks",  # create one or more tasks (params in ``tasks``)
    "list_tasks",    # summary of the existing tasks
    "list_projects",  # distinct work directories present in the tasks (global scope)
    "list_project_tasks",  # tasks of ONE project (``project_ref``)
    "task_status",   # status of one task (``task_ref``)
    "send_files",    # send the task .md artifacts (``task_ref``)
    "ask",           # answer a question about a task (``task_ref`` + ``question``)
    "help",          # usage help
    "unknown",       # could not understand
)

MAX_TASKS_PER_INTENT = 10
PARSER_TIMEOUT = 120.0  # seconds; a hung parser CLI must not wedge the bot
PARSER_RETRIES = 2      # extra attempts after a transient parser CLI failure
PARSER_RETRY_DELAY = 5.0       # seconds between attempts without a wait hint
PARSER_RETRY_MAX_WAIT = 90.0   # cap for a usage "retry-after" hint
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
    task_ref: str = ""      # id or name fragment the user refers to
    question: str = ""      # question for the "ask" action
    project_ref: str = ""   # directory or name fragment of a project (list_project_tasks)
    error: str = ""         # parser CLI infrastructure failure (not "unknown")
    lang: str = ""          # ISO code of the user's language ("" = unknown)


def tasks_summary(tasks: list[Task], *, limit: int = _SUMMARY_LIMIT) -> str:
    """Compact ``- id | name | state | workdir`` listing used as parser context.

    The workdir column lets the parser route new tasks to the right project
    and disambiguate task references; remote tasks show their SSH spec.
    """
    lines = [
        f"- {task.id} | {task.name} | {_prompt_state_label(task)} | {_task_dir(task)}"
        for task in tasks[:limit]
    ]
    return "\n".join(lines)


def _prompt_state_label(task: Task) -> str:
    """State label in the prompt language (the listing is parser context)."""
    return t_lang(prompt_language(), f"state.{task.state.value}")


def _task_dir(task: Task) -> str:
    """Directory shown in the parser context: SSH spec for remote tasks."""
    return task.remote if task.is_remote else task.workdir


def _merge_discovered(counts: dict[str, int], extra_dirs: Iterable[str]) -> list[tuple[str, int]]:
    """``counts`` items plus discovered dirs not already covered, with count 0.

    A discovered dir is covered when it matches an existing entry as an
    exact string or as the same resolved local path (remote SSH specs
    never cover local dirs).
    """
    items = list(counts.items())
    for raw in extra_dirs:
        candidate = str(raw)
        covered = False
        for directory, _ in items:
            if directory == candidate:
                covered = True
                break
            if "@" not in directory and "@" not in candidate:
                try:
                    if Path(directory).resolve() == Path(candidate).expanduser().resolve():
                        covered = True
                        break
                except OSError:
                    pass
        if not covered:
            items.append((candidate, 0))
    return items


def _match_dir(needle: str, directories: list[str]) -> str | None:
    """Exact (case-insensitive) or unique substring/basename match."""
    for directory in directories:
        if directory.lower() == needle:
            return directory
    matches = [
        directory
        for directory in directories
        if needle in directory.lower()
        or needle in Path(directory.rstrip("/")).name.lower()
    ]
    return matches[0] if len(matches) == 1 else None


def project_dirs(tasks: list[Task], extra_dirs: Iterable[str] = ()) -> list[tuple[str, int]]:
    """Distinct task directories with their task count, in first-seen order.

    The directory is the remote SSH spec for remote tasks (same rule as
    ``tasks_summary``). ``tasks`` comes from ``models.list_all`` (newest
    first), so the most recently used projects appear first. A plain dict
    keeps insertion order, which gives the grouping in a single pass.
    ``extra_dirs`` holds discovered workspace projects: those not already
    covered by a task directory are appended at the end with count 0.
    """
    counts: dict[str, int] = {}
    for task in tasks:
        directory = _task_dir(task)
        counts[directory] = counts.get(directory, 0) + 1
    return _merge_discovered(counts, extra_dirs)


def projects_summary(tasks: list[Task], extra_dirs: Iterable[str] = ()) -> str:
    """Compact ``- dir | count`` listing of project dirs (parser context)."""
    return "\n".join(
        f"- {directory} | {count}"
        for directory, count in project_dirs(tasks, extra_dirs)
    )


_PARSER_PROMPT = {
    "es": """Eres el interpretador de mensajes de GRAFENO, un orquestador de tareas de
programación. El usuario escribe o dicta por voz mensajes para crear tareas o
consultar las existentes.

Tareas existentes (id | nombre | estado | directorio):
{summary}

Proyectos con tareas (directorio | nº tareas):
{projects}

Directorio de trabajo por defecto para tareas nuevas: {default_workdir}

Mensaje del usuario:
\"\"\"
{user_text}
\"\"\"

Responde SOLO con un objeto JSON (sin texto alrededor, sin Markdown) con esta forma:
{{
  "action": "create_tasks" | "list_tasks" | "list_projects" | "list_project_tasks" | "task_status" | "send_files" | "ask" | "help" | "unknown",
  "tasks": [{{"name": "...", "description": "...", "workdir": "...", "test_command": "..."}}],
  "task_ref": "id o fragmento del nombre de la tarea (para task_status, send_files, ask)",
  "project_ref": "directorio del proyecto (solo para list_project_tasks)",
  "question": "la pregunta concreta del usuario (solo para ask)",
  "lang": "código ISO 639-1 del idioma del mensaje del usuario (es, en, ...)"
}}

Reglas:
- "lang": SIEMPRE el idioma en que el usuario escribió o dictó el mensaje.
- "create_tasks": una entrada por cada tarea que pida el mensaje. name corto
  pero CONCRETO y específico (nunca genérico: evita títulos tipo "cambios en
  el código" o "nueva tarea"; nombra el objetivo exacto). description con TODA
  la información sustantiva del mensaje: requisitos, restricciones, cantidades,
  nombres de archivos, funciones o módulos, condiciones, casos límite,
  ejemplos y matices que aporte el usuario. PROHIBIDO resumir de forma agresiva,
  condensar o fusionar requisitos: la descripción debe heredar la extensión
  del mensaje, y una transcripción de audio larga y estructurada justifica una
  descripción igualmente larga (varios párrafos o listas si hace falta). Si el
  mensaje viene de una nota de voz transcrita, conserva los detalles con la
  precisión del texto transcrito, sin re-redactarlo en versión corta. Omite
  solo muletillas y rellenos propios del audio hablado.
  Para "workdir": si el mensaje se refiere a un proyecto del listado de
  proyectos o con tareas existentes (por nombre de proyecto o de
  directorio), usa EXACTAMENTE el directorio de ese listado (sin inventar
  rutas); si el usuario indica una ruta explícita, úsala tal cual; si no se
  puede determinar el proyecto, déjalo vacío (se usará el directorio por defecto).
  Las rutas "user@host:..." son proyectos remotos: no las uses para tareas nuevas.
  test_command solo si lo indica.
- Si el mensaje pide varias tareas, inclúyelas todas en "tasks".
- "list_tasks": el usuario quiere un resumen de sus tareas.
- "list_projects": el usuario pide el listado de PROYECTOS o directorios en
  los que hay tareas (no el listado de tareas): "qué proyectos tengo",
  "lista los directorios", "en qué proyectos estoy trabajando".
- "list_project_tasks": el usuario pide las tareas de UN solo proyecto:
  "que tareas tiene el proyecto X", "lista las tareas de grafeno",
  "tareas de /ruta/al/proyecto". Devuelve en "project_ref" EXACTAMENTE el
  directorio de ese proyecto tal como aparece en el listado de proyectos
  (tambien vale el spec "user@host:..." de un proyecto remoto); si no se
  puede determinar el proyecto, deja "project_ref" vacio.
- "task_status": pregunta por el estado de una tarea concreta.
- "send_files": el usuario quiere que le envíes los archivos .md resultantes de
  una tarea (plan, revisiones, informe final).
- "ask": cualquier otra pregunta sobre una tarea concreta.
- "help": pide ayuda o no queda claro qué hacer.
- "unknown": no se puede interpretar.
- Cuando el mensaje sea largo o muy detallado (típico de audios dictados), NO comprimas la
  descripción por brevedad: prioriza no perder información relevante sobre la brevedad. Lo
  genérico y vago es peor que lo largo.
- No inventes tareas: crea solo lo que el mensaje pida explícitamente.
- Usa el listado de tareas (id | nombre | estado | directorio) para resolver
  "task_ref" de forma inequívoca: devuelve el id exacto de la tarea que mejor
  encaje con lo que pide el usuario; el directorio ayuda a distinguir tareas
  con nombres parecidos en proyectos distintos.
""",
    "en": """You are the message interpreter of GRAFENO, a programming task
orchestrator. The user writes or dictates by voice messages to create tasks
or query the existing ones.

Existing tasks (id | name | state | directory):
{summary}

Projects with tasks (directory | no. of tasks):
{projects}

Default working directory for new tasks: {default_workdir}

User message:
\"\"\"
{user_text}
\"\"\"

Answer ONLY with a JSON object (no surrounding text, no Markdown) with this shape:
{{
  "action": "create_tasks" | "list_tasks" | "list_projects" | "list_project_tasks" | "task_status" | "send_files" | "ask" | "help" | "unknown",
  "tasks": [{{"name": "...", "description": "...", "workdir": "...", "test_command": "..."}}],
  "task_ref": "id or fragment of the task name (for task_status, send_files, ask)",
  "project_ref": "project directory (only for list_project_tasks)",
  "question": "the user's concrete question (only for ask)",
  "lang": "ISO 639-1 code of the language of the user's message (es, en, ...)"
}}

Rules:
- "lang": ALWAYS the language in which the user wrote or dictated the message.
- "create_tasks": one entry per task the message asks for. name short
  but CONCRETE and specific (never generic: avoid titles like "code
  changes" or "new task"; name the exact goal). description with ALL
  the substantive information of the message: requirements, constraints,
  quantities, names of files, functions or modules, conditions, edge cases,
  examples and nuances provided by the user. It is FORBIDDEN to summarize
  aggressively, condense or merge requirements: the description must inherit
  the length of the message, and a long, structured audio transcription
  justifies an equally long description (several paragraphs or lists if
  needed). If the message comes from a transcribed voice note, keep the
  details with the precision of the transcribed text, without rewriting it as
  a short version. Omit only the filler words typical of spoken audio.
  For "workdir": if the message refers to a project of the projects listing
  or with existing tasks (by project or directory name), use EXACTLY the
  directory of that listing (without inventing paths); if the user gives an
  explicit path, use it as is; if the project cannot be determined, leave it
  empty (the default directory will be used).
  "user@host:..." paths are remote projects: do not use them for new tasks.
  test_command only if the user states it.
- If the message asks for several tasks, include all of them in "tasks".
- "list_tasks": the user wants a summary of their tasks.
- "list_projects": the user asks for the listing of PROJECTS or directories
  that have tasks (not the task listing): "what projects do I have",
  "list the directories", "which projects am I working on".
- "list_project_tasks": the user asks for the tasks of ONE single project:
  "what tasks does project X have", "list the tasks of grafeno",
  "tasks of /path/to/project". Return in "project_ref" EXACTLY the
  directory of that project as it appears in the projects listing
  (the "user@host:..." spec of a remote project is also valid); if the
  project cannot be determined, leave "project_ref" empty.
- "task_status": asks about the state of a specific task.
- "send_files": the user wants you to send the resulting .md files of
  a task (plan, reviews, final report).
- "ask": any other question about a specific task.
- "help": asks for help or it is unclear what to do.
- "unknown": the message cannot be interpreted.
- When the message is long or very detailed (typical of dictated audio), do NOT
  compress the description for brevity: prioritize not losing relevant
  information over brevity. Generic and vague is worse than long.
- Do not invent tasks: create only what the message explicitly asks for.
- Use the task listing (id | name | state | directory) to resolve
  "task_ref" unambiguously: return the exact id of the task that best
  matches what the user asks for; the directory helps to tell apart tasks
  with similar names in different projects.
""",
}
_PARSER_NO_TASKS = {"es": "(ninguna)", "en": "(none)"}
_PARSER_NO_PROJECTS = {"es": "(ninguno)", "en": "(none)"}


def build_parser_prompt(
    user_text: str,
    summary: str,
    default_workdir: str,
    projects: str = "",
) -> str:
    """One-shot prompt: interpret the user message and answer with strict JSON."""
    lang = prompt_language()
    return prompt_template(_PARSER_PROMPT, lang).format(
        summary=summary or _PARSER_NO_TASKS[lang],
        projects=projects or _PARSER_NO_PROJECTS[lang],
        default_workdir=default_workdir or ".",
        user_text=user_text,
    )


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
        project_ref=str(payload.get("project_ref", "") or "").strip(),
        lang=_parse_lang(payload.get("lang")),
    )
    if intent.action == "create_tasks" and not intent.tasks:
        intent.action = "unknown"  # nothing valid to create
    if intent.action in ("task_status", "send_files", "ask") and not intent.task_ref:
        intent.action = "help"  # no target task: better to explain usage
    if intent.action == "list_project_tasks" and not intent.project_ref:
        intent.action = "help"  # no target project: better to explain usage
    return intent


async def parse_intent(
    driver: CLIDriver,
    model: str,
    user_text: str,
    summary: str,
    workdir: Path,
    *,
    default_workdir: str = "",
    projects: str = "",
    timeout: float = PARSER_TIMEOUT,
    retries: int = PARSER_RETRIES,
    retry_delay: float = PARSER_RETRY_DELAY,
) -> Intent:
    """Run the parser CLI one-shot and interpret its JSON answer.

    Infrastructure failures (timeout, crash, non-zero exit, empty output) are
    retried a few times — they are usually transient (provider hiccup, quota
    reset) and the one-shot has no side effects. A usage-limit hint
    (``RunResult.usage_wait``) sets the wait between attempts, capped at
    ``PARSER_RETRY_MAX_WAIT`` so the chat is not held for long. The last
    failure is reported in ``Intent.error`` so the caller can tell it apart
    from a genuine "unknown" intent and surface it instead of staying silent.
    """
    prompt = build_parser_prompt(user_text, summary, default_workdir, projects)
    request = RunRequest(
        prompt=prompt,
        model=model,
        workdir=workdir,
        title="grafeno:telegram:intent",
    )
    attempts = 1 + max(0, retries)
    error = ""
    for attempt in range(attempts):
        wait_hint: float | None = None
        try:
            result = await asyncio.wait_for(driver.run(request), timeout=timeout)
        except TimeoutError:
            error = f"timeout after {timeout:.0f}s"
        except Exception as exc:  # noqa: BLE001 - the bot never propagates CLI errors
            error = str(exc)[:300]
        else:
            if result.ok:
                if not result.text.strip():
                    error = "empty output"
                else:
                    return parse_intent_payload(result.text)
            else:
                error = (result.error or "exit error")[:1000]
                wait_hint = result.usage_wait
        if attempt >= attempts - 1:
            break
        delay = retry_delay
        if wait_hint is not None and wait_hint > 0:
            delay = min(wait_hint, PARSER_RETRY_MAX_WAIT)
        await asyncio.sleep(delay)
    return Intent(action="unknown", error=error)


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


def resolve_workdir(
    spec_workdir: str,
    tasks: list[Task],
    default: str = "",
    extra_dirs: Iterable[str] = (),
) -> str:
    """Canonical workdir for a parsed TaskSpec.

    Empty means "not determined" and falls back to the default directory
    (current behavior). A value matching the directory of an existing task
    (local workdir or remote spec) is normalized to that exact string; if no
    task matches, discovered workspace projects (``extra_dirs``) are tried by
    exact path or folder name. Any other value is passed through stripped:
    the service validates that the directory exists, as it does today for
    explicit paths.
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
    discovered = _match_dir(lowered, [str(item) for item in extra_dirs])
    if discovered is not None:
        return discovered
    return candidate


def resolve_project_dir(
    ref: str,
    tasks: list[Task],
    extra_dirs: Iterable[str] = (),
) -> str | None:
    """Best match for a project reference against the known project dirs.

    Order: exact (case-insensitive) directory match, then a unique
    case-insensitive substring match on the full directory or its basename
    (the project "name"). ``project_dirs`` keeps first-seen order (most
    recently used first) and appends the discovered workspace projects in
    ``extra_dirs``. An ambiguous fragment returns None so the caller can ask
    the user to be more specific instead of guessing wrong.
    """
    needle = ref.strip().lower()
    if not needle:
        return None
    directories = [directory for directory, _ in project_dirs(tasks, extra_dirs)]
    return _match_dir(needle, directories)


def project_tasks(directory: str, tasks: list[Task]) -> list[Task]:
    """Tasks whose directory (workdir or remote SSH spec) is exactly ``directory``."""
    return [task for task in tasks if _task_dir(task) == directory]
