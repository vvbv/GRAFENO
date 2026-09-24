"""Prompt templates for each phase of the GRAFENO pipeline.

Every plan includes a ``GRAFENO-EXECUTOR`` header declaring which model and
which CLI will implement it, and the planner is required to optimize the
content for that executor (configuration modes 1 and 2).

Each template is a per-language dict (``{"es": ..., "en": ...}``, both
variants side by side so translations stay in sync) rendered in the
configured prompt language (``i18n.prompt_language()``: the GUI language
unless the settings pick a separate one). Templates use ``str.format``
placeholders and are formatted exactly once, so braces in task data are safe.
"""

from __future__ import annotations

from ..i18n import prompt_language, prompt_template
from ..models import Task
from ..references import resolve
from .. import media, paths

EXECUTOR_HEADER_TEMPLATE = """<!-- GRAFENO-EXECUTOR
cli: {cli}
model: {model}
-->"""

EXECUTOR_NOTICE_TEMPLATE = {
    "es": (
        "> **Ejecutor de este plan**: lo implementará el modelo `{model}` "
        "a través del CLI `{cli}`. Este plan está optimizado para ese ejecutor."
    ),
    "en": (
        "> **Executor of this plan**: it will be implemented by the model `{model}` "
        "through the CLI `{cli}`. This plan is optimized for that executor."
    ),
}

_COMMON_RULES = {
    "es": """
Reglas de esta ejecución (modo automático, no interactivo):
- NO hagas preguntas: decide con criterio y actúa.
- Trabaja siempre dentro del directorio del proyecto indicado.
- Responde de forma breve; los artefactos importantes van en los archivos.
- Si la tarea requiere operaciones de git (commit, push, tags, etc.), usa el
  autor y correo ya configurados en el sistema donde se ejecuta
  (`git config user.name` / `git config user.email`): bajo ningún concepto los
  modifiques, salvo que la tarea lo solicite explícitamente.
- Los mensajes de commit se escriben SIEMPRE en INGLÉS por defecto, salvo que
  el AGENTS.md del proyecto o los datos de la tarea indiquen lo contrario.
""".strip(),
    "en": """
Rules for this run (automatic, non-interactive mode):
- Do NOT ask questions: decide with judgment and act.
- Always work inside the given project directory.
- Answer briefly; the important artifacts go in the files.
- If the task requires git operations (commit, push, tags, etc.), use the
  author and email already configured on the system where it runs
  (`git config user.name` / `git config user.email`): under no circumstances
  modify them, unless the task explicitly asks for it.
- Commit messages are ALWAYS written in ENGLISH by default, unless the
  project's AGENTS.md or the task data say otherwise.
""".strip(),
}

_CODE_RULES = {
    "es": """
Reglas de código (obligatorias al implementar):
- Nada de emotes/emojis en el código, los comentarios ni la documentación.
- La documentación, los comentarios y los nombres de métodos/funciones/clases
  se escriben en INGLÉS, salvo que el proyecto tenga de forma considerable
  otro idioma dominante: en ese caso, sigue el estilo de codificación ya
  existente en el proyecto.
""".strip(),
    "en": """
Code rules (mandatory when implementing):
- No emotes/emojis in the code, the comments or the documentation.
- Documentation, comments and method/function/class names are written in
  ENGLISH, unless the project has a considerably dominant other language:
  in that case, follow the coding style already present in the project.
""".strip(),
}

_MD_RULES = {
    "es": """
Formato Markdown (obligatorio en los archivos .md que generes):
- Listas compactas: nunca dejes lineas en blanco entre elementos consecutivos
  de una misma lista (ni con guiones, ni numeradas, ni checkboxes).
- No uses mas de UNA linea en blanco seguida para separar bloques.
""".strip(),
    "en": """
Markdown format (mandatory in the .md files you generate):
- Compact lists: never leave blank lines between consecutive items of the
  same list (dashes, numbered or checkboxes).
- Do not use more than ONE consecutive blank line to separate blocks.
""".strip(),
}

_NO_DESCRIPTION = {"es": "(sin descripción)", "en": "(no description)"}


def executor_header(task: Task) -> str:
    return EXECUTOR_HEADER_TEMPLATE.format(cli=task.implementer.cli, model=task.implementer.model or "default")


def executor_notice(task: Task, lang: str = "") -> str:
    return prompt_template(EXECUTOR_NOTICE_TEMPLATE, lang).format(
        cli=task.implementer.cli, model=task.implementer.model or "default"
    )


def _fields(task: Task, lang: str) -> dict[str, object]:
    """Placeholders shared by the phase templates."""
    return {
        "name": task.name,
        "description": task.description or _NO_DESCRIPTION[lang],
        "workdir": task.workdir,
        "cli": task.implementer.cli,
        "model": task.implementer.model or "default",
        "code_rules": _CODE_RULES[lang],
        "md_rules": _MD_RULES[lang],
        "common_rules": _COMMON_RULES[lang],
    }


def _render(templates: dict[str, str], lang: str, **fields: object) -> str:
    return prompt_template(templates, lang).format(**fields).strip()


_TESTS_SECTION = {
    "es": (
        "\n- La tarea define un comando de tests: `{command}`. "
        "Incluye en el plan cómo satisfacerlo."
    ),
    "en": (
        "\n- The task defines a test command: `{command}`. "
        "Include in the plan how to satisfy it."
    ),
}


def _tests_section(task: Task, lang: str) -> str:
    if not task.test_command:
        return ""
    return prompt_template(_TESTS_SECTION, lang).format(command=task.test_command)


_CYCLE_SECTION = {
    "es": """
# Ampliación (ciclo {cycle})
Esta tarea ya completó ciclos anteriores: el proyecto contiene ese trabajo.
Nueva petición del usuario sobre ese trabajo:
{extension}

Planifica SOLO esta ampliación: no repitas lo ya implementado.
""",
    "en": """
# Extension (cycle {cycle})
This task already completed previous cycles: the project contains that work.
New user request on top of that work:
{extension}

Plan ONLY this extension: do not repeat what is already implemented.
""",
}
_NO_DETAIL = {"es": "(sin detalle)", "en": "(no details)"}


def _cycle_section(task: Task, lang: str) -> str:
    """Extension context when the task is in cycle >= 2."""
    if task.cycle <= 1:
        return ""
    return prompt_template(_CYCLE_SECTION, lang).format(
        cycle=task.cycle, extension=task.current_extension or _NO_DETAIL[lang]
    )


_REFERENCES_SECTION = {
    "es": """
# Referencias de contexto
La tarea define los siguientes recursos de referencia. Revísalos (explora el
directorio local o consulta la URL) y úsalos SOLO como contexto o inspiración
para lo que indica su descripción; no los copies literalmente ni los trates
como parte del proyecto salvo que la descripción lo pida. Sé selectivo con lo
que lees de cada recurso: leer un recurso grande por completo incrementa
mucho el consumo de tokens.
{items}
""",
    "en": """
# Context references
The task defines the following reference resources. Review them (explore the
local directory or check the URL) and use them ONLY as context or inspiration
for what their description says; do not copy them literally or treat them
as part of the project unless the description asks for it. Be selective with
what you read from each resource: reading a large resource in full greatly
increases token consumption.
{items}
""",
}


def _references_section(task: Task, lang: str) -> str:
    """Context references section (empty if the task has none)."""
    references = resolve(task)
    if not references:
        return ""
    items = "\n".join(
        f"- **{ref.name}** ({ref.path}): {ref.description or _NO_DESCRIPTION[lang]}"
        for ref in references
    )
    return prompt_template(_REFERENCES_SECTION, lang).format(items=items)


_MEDIA_SECTION = {
    "es": "\n\nImágenes adjuntas a la tarea (puedes leerlas si tu CLI lo soporta):\n{lines}\n",
    "en": "\n\nImages attached to the task (you can read them if your CLI supports it):\n{lines}\n",
}


def _media_section(task: Task, lang: str) -> str:
    """Attached images (absolute paths so vision-capable CLIs can read them)."""
    files = media.list_media(task.id)
    if not files:
        return ""
    lines = "\n".join(f"- {path}" for path in files)
    return prompt_template(_MEDIA_SECTION, lang).format(lines=lines)


_REMOTE_SECTION = {
    "es": """
# Entorno remoto (SSH)
Esta tarea trabaja sobre un proyecto remoto; tenlo en cuenta en TODO comando:
- Conexión: `{spec_label}` (el directorio local de trabajo es un montaje sshfs)
- SO en el destino: {os_label}
- Las pruebas o comandos que dependan del SO (shell, rutas, binarios) deben
  pensarse para el destino; cuando convenga, ejecútalos con `ssh {target} <cmd>`.
{mandate}""",
    "en": """
# Remote environment (SSH)
This task works on a remote project; keep it in mind in EVERY command:
- Connection: `{spec_label}` (the local working directory is an sshfs mount)
- OS on the target: {os_label}
- Tests or commands that depend on the OS (shell, paths, binaries) must be
  designed for the target; when appropriate, run them with `ssh {target} <cmd>`.
{mandate}""",
}
_REMOTE_OS_UNKNOWN = {
    "es": "no detectado (compruébalo si es relevante)",
    "en": "not detected (check it if relevant)",
}
_REMOTE_PLAN_MANDATE = {
    "es": (
        "- El plan DEBE comenzar con una sección \"Entorno remoto\" que fije el SO\n"
        "  de destino y sus implicaciones: shell, rutas, binarios disponibles y cómo\n"
        "  ejecutar pruebas o comandos en el remoto cuando proceda.\n"
    ),
    "en": (
        "- The plan MUST start with a \"Remote environment\" section that pins down the\n"
        "  target OS and its implications: shell, paths, available binaries and how\n"
        "  to run tests or commands on the remote when appropriate.\n"
    ),
}


def _remote_section(task: Task, lang: str, *, for_plan: bool = False) -> str:
    """Remote environment section (empty for plain local tasks)."""
    # Import locally to avoid any import cycle during pipeline startup.
    from .. import remotesession

    session = remotesession.current()
    if not task.is_remote and session is None:
        return ""
    if task.is_remote:
        spec_label = task.remote
        target = task.remote.split(":", 1)[0]  # user@host
    else:
        target = session.spec.target
        spec_label = f"{target}:{task.workdir}"
    os_label = (
        task.remote_os
        or (session.remote_os if session else "")
        or _REMOTE_OS_UNKNOWN[lang]
    )
    return prompt_template(_REMOTE_SECTION, lang).format(
        spec_label=spec_label,
        os_label=os_label,
        target=target,
        mandate=_REMOTE_PLAN_MANDATE[lang] if for_plan else "",
    )


_CUSTOM_FINAL_SECTION = {
    "es": """
# Instrucciones adicionales del usuario para el cierre
{instructions}
""",
    "en": """
# Additional user instructions for the wrap-up
{instructions}
""",
}


def _custom_final_section(task: Task, lang: str) -> str:
    """Extra user instructions for the final phase (empty = no section)."""
    if not task.final_prompt.strip():
        return ""
    return prompt_template(_CUSTOM_FINAL_SECTION, lang).format(
        instructions=task.final_prompt.strip()
    )


_FIRST_PROMPT = {
    "es": """Eres el AGENTE DE PRIMER PASO de una tarea orquestada por GRAFENO.
Tu trabajo se ejecuta ANTES de la planificación: prepara lo que indiquen las
instrucciones iniciales de la tarea, ni más ni menos.

# Contexto
- Tarea: {name}
- Descripción: {description}
- Proyecto (directorio de trabajo): {workdir}
{remote}
# Instrucciones del primer paso (definidas por el usuario)
{first_instructions}

# Tu entrega
1. Ejecuta las instrucciones del primer paso sin salirte de su alcance.
2. NO escribas el plan ni implementes la tarea: eso viene después con otros agentes.
3. Escribe el resultado en el archivo:
   {first_file}
   con secciones: Resumen, Acciones realizadas, Observaciones.

{code_rules}

{md_rules}

Termina tu respuesta con un resumen de las acciones realizadas.

{common_rules}
""",
    "en": """You are the FIRST-STEP AGENT of a task orchestrated by GRAFENO.
Your work runs BEFORE planning: prepare what the initial instructions of the
task say, no more and no less.

# Context
- Task: {name}
- Description: {description}
- Project (working directory): {workdir}
{remote}
# First-step instructions (defined by the user)
{first_instructions}

# Your deliverable
1. Carry out the first-step instructions without going beyond their scope.
2. Do NOT write the plan or implement the task: that comes later with other agents.
3. Write the result to the file:
   {first_file}
   with sections: Summary, Actions taken, Observations.

{code_rules}

{md_rules}

End your answer with a summary of the actions taken.

{common_rules}
""",
}


def first_prompt(task: Task) -> str:
    """First-step prompt: initial instructions defined by the user."""
    lang = prompt_language()
    return _render(
        _FIRST_PROMPT,
        lang,
        **_fields(task, lang),
        remote=_remote_section(task, lang),
        first_instructions=task.first_prompt.strip(),
        first_file=paths.first_dir(task.id, task.cycle) / "01-first.md",
    )


_PLAN_PROMPT = {
    "es": """Eres un INGENIERO DE SOFTWARE SENIOR actuando como PLANIFICADOR de una
tarea de programación orquestada por GRAFENO.

# Tarea
- Nombre: {name}
- Descripción: {description}
- Proyecto (directorio de trabajo): {workdir}
{cycle}{remote}{references}{media}
# Tu entrega
1. Explora el proyecto para entender su estructura, stack y convenciones.
2. DESCUBRIMIENTO OBLIGATORIO — antes de diseñar la solución:
   - Antes de proponer cualquier helper, utilidad, componente UI, formato o
     patrón nuevo, DEMUESTRA si el proyecto ya lo resuelve: busca por el
     concepto, no solo por el nombre exacto (p.ej. mask/phone/format/clean si
     la tarea es de formato), y cita la evidencia (comando + archivo + línea).
   - Revisa EXPLÍCITAMENTE lo que las plantillas base y el framework ya cargan
     (librerías JS/CSS incluidas en el layout base, helpers JS globales,
     helpers de render del backend, registries, hooks, filtros) y cómo las
     secciones/módulos similares resuelven lo mismo: un mecanismo ya
     implementado en otra parte del proyecto se REUTILIZA, no se reinventa.
   - El plan DEBE incluir una sección breve "Capacidades existentes detectadas"
     con lo que se reutiliza (componente → archivo → cómo se consume). Si el
     diseño propone algo nuevo, justifica ahí que ninguna capacidad existente
     cubre el caso; suponer que no existe sin haberlo verificado ES un defecto
     del plan.
3. Escribe el plan en UNO O VARIOS archivos Markdown dentro de:
   {plan_dir}
   Nómbralos `NN-slug.md` (p.ej. `01-setup.md`, `02-api.md`) en orden de ejecución.
4. CADA archivo debe comenzar EXACTAMENTE con esta cabecera (sin modificar):

{executor_header}
{executor_notice}

5. El plan lo ejecutará OTRO modelo (`{model}` vía CLI `{cli}`),
   que no compartirá tu contexto. OPTIMIZA el plan para que ese ejecutor lo
   implemente sin ambigüedades:
   - pasos pequeños, numerados y verificables;
   - rutas de archivos exactas a crear o modificar;
   - comandos concretos listos para copiar;
   - fragmentos de código clave cuando aporten claridad;
   - criterios de aceptación explícitos al final de cada archivo.{tests}
   - Si la tarea es visual (máscara, layout, formato, feedback de UI), los
     criterios DEBEN incluir el recorrido manual verificable (p.ej. smoke test
     con sesión activa); el análisis estático no basta para aprobarlos.
6. Para cualquier método o función que consideres COMPLEJO para el modelo que
   implementará (`{model}` vía `{cli}`),
   añade un bloque "Sugerencias" junto a ese paso con:
   - descomposición del método en funciones más pequeñas, si aplica;
   - pseudocódigo o la firma exacta del método;
   - alternativas más simples de implementar y advertencias de errores típicos.
7. NO implementes el código: solo planifica.
8. El plan debe incluir literalmente estas reglas para el ejecutor:

{code_rules}

{md_rules}

9. Termina tu respuesta con un resumen de 3 líneas y la lista de archivos escritos.

{common_rules}
""",
    "en": """You are a SENIOR SOFTWARE ENGINEER acting as the PLANNER of a
programming task orchestrated by GRAFENO.

# Task
- Name: {name}
- Description: {description}
- Project (working directory): {workdir}
{cycle}{remote}{references}{media}
# Your deliverable
1. Explore the project to understand its structure, stack and conventions.
2. MANDATORY DISCOVERY — before designing the solution:
   - Before proposing any new helper, utility, UI component, format or
     pattern, PROVE whether the project already solves it: search by the
     concept, not only by the exact name (e.g. mask/phone/format/clean if
     the task is about formatting), and cite the evidence (command + file + line).
   - Review EXPLICITLY what the base templates and the framework already load
     (JS/CSS libraries included in the base layout, global JS helpers,
     backend render helpers, registries, hooks, filters) and how similar
     sections/modules solve the same thing: a mechanism already
     implemented elsewhere in the project is REUSED, not reinvented.
   - The plan MUST include a short "Detected existing capabilities" section
     with what is reused (component → file → how it is consumed). If the
     design proposes something new, justify there that no existing capability
     covers the case; assuming it does not exist without having verified it
     IS a defect of the plan.
3. Write the plan in ONE OR MORE Markdown files inside:
   {plan_dir}
   Name them `NN-slug.md` (e.g. `01-setup.md`, `02-api.md`) in execution order.
4. EACH file must start EXACTLY with this header (unmodified):

{executor_header}
{executor_notice}

5. The plan will be executed by ANOTHER model (`{model}` via CLI `{cli}`),
   which will not share your context. OPTIMIZE the plan so that executor
   implements it without ambiguity:
   - small, numbered and verifiable steps;
   - exact paths of the files to create or modify;
   - concrete commands ready to copy;
   - key code snippets when they add clarity;
   - explicit acceptance criteria at the end of each file.{tests}
   - If the task is visual (mask, layout, format, UI feedback), the
     criteria MUST include the verifiable manual walkthrough (e.g. smoke test
     with an active session); static analysis is not enough to approve them.
6. For any method or function you consider COMPLEX for the model that will
   implement it (`{model}` via `{cli}`),
   add a "Suggestions" block next to that step with:
   - breakdown of the method into smaller functions, if applicable;
   - pseudocode or the exact signature of the method;
   - simpler alternatives to implement and warnings about typical mistakes.
7. Do NOT implement the code: only plan.
8. The plan must literally include these rules for the executor:

{code_rules}

{md_rules}

9. End your answer with a 3-line summary and the list of files written.

{common_rules}
""",
}


def plan_prompt(task: Task) -> str:
    lang = prompt_language()
    return _render(
        _PLAN_PROMPT,
        lang,
        **_fields(task, lang),
        cycle=_cycle_section(task, lang),
        remote=_remote_section(task, lang, for_plan=True),
        references=_references_section(task, lang),
        media=_media_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        executor_header=executor_header(task),
        executor_notice=executor_notice(task, lang),
        tests=_tests_section(task, lang),
    )


_REEVALUATE_PLAN_PROMPT = {
    "es": """Eres un INGENIERO DE SOFTWARE SENIOR actuando como PLANIFICADOR de una
tarea de programación orquestada por GRAFENO. Esta es una REEVALUACIÓN de un
plan existente, NO una planificación desde cero.

# Tarea
- Nombre: {name}
- Descripción: {description}
- Proyecto (directorio de trabajo): {workdir}
{cycle}{remote}{references}{media}
# Tu entrega
1. La tarea YA TIENE archivos de plan en:
   {plan_dir}
   LEE PRIMERO esos archivos (en orden alfabético) para entender qué se
   planificó en repeticiones anteriores.
2. Audita el DESCUBRIMIENTO de la planificación previa: si propuso un helper,
   utilidad, componente UI, formato o patrón nuevo sin verificar (con
   evidencia de comandos) si el proyecto ya lo resuelve, repite esa revisión
   ahora y CORRÍGELO para reutilizar la capacidad existente; la sección
   "Capacidades existentes detectadas" del plan debe quedar respondida con la
   misma exigencia que en una planificación nueva.
3. Compara el plan existente con la descripción ORIGINAL de la tarea (arriba)
   y con el estado actual del proyecto en `{workdir}`.
4. ACTUALIZA los archivos del plan solo donde proceda:
   - mantén el formato de cabecera EXACTO en cada archivo (sin modificar):

{executor_header}
{executor_notice}

   - ajusta pasos, criterios de aceptación y sugerencias a la realidad del
     proyecto;
   - añade o elimina archivos `NN-slug.md` si la estructura del plan cambia;
   - mantén el orden de numeración coherente.
5. Si el plan sigue siendo válido, NO lo modifiques: indícalo en tu respuesta
   y no escribas archivos nuevos.
6. El plan lo ejecutará OTRO modelo (`{model}` vía CLI `{cli}`),
   que no compartirá tu contexto. OPTIMIZA el plan para ese ejecutor.{tests}
7. El plan debe incluir literalmente estas reglas para el ejecutor:

{code_rules}

{md_rules}

8. Termina tu respuesta con un resumen de los cambios (o de "sin cambios").

{common_rules}
""",
    "en": """You are a SENIOR SOFTWARE ENGINEER acting as the PLANNER of a
programming task orchestrated by GRAFENO. This is a RE-EVALUATION of an
existing plan, NOT planning from scratch.

# Task
- Name: {name}
- Description: {description}
- Project (working directory): {workdir}
{cycle}{remote}{references}{media}
# Your deliverable
1. The task ALREADY HAS plan files in:
   {plan_dir}
   READ those files FIRST (in alphabetical order) to understand what was
   planned in previous repetitions.
2. Audit the DISCOVERY of the previous planning: if it proposed a new helper,
   utility, UI component, format or pattern without verifying (with
   command evidence) whether the project already solves it, repeat that
   review now and FIX IT to reuse the existing capability; the plan's
   "Detected existing capabilities" section must be answered with the
   same rigor as in a new planning.
3. Compare the existing plan with the ORIGINAL task description (above)
   and with the current state of the project in `{workdir}`.
4. UPDATE the plan files only where appropriate:
   - keep the EXACT header format in each file (unmodified):

{executor_header}
{executor_notice}

   - adjust steps, acceptance criteria and suggestions to the reality of the
     project;
   - add or remove `NN-slug.md` files if the structure of the plan changes;
   - keep the numbering order consistent.
5. If the plan is still valid, do NOT modify it: say so in your answer
   and do not write new files.
6. The plan will be executed by ANOTHER model (`{model}` via CLI `{cli}`),
   which will not share your context. OPTIMIZE the plan for that executor.{tests}
7. The plan must literally include these rules for the executor:

{code_rules}

{md_rules}

8. End your answer with a summary of the changes (or of "no changes").

{common_rules}
""",
}


def reevaluate_plan_prompt(task: Task) -> str:
    """Re-evaluation prompt: adjusts the existing plan to the description."""
    lang = prompt_language()
    return _render(
        _REEVALUATE_PLAN_PROMPT,
        lang,
        **_fields(task, lang),
        cycle=_cycle_section(task, lang),
        remote=_remote_section(task, lang, for_plan=True),
        references=_references_section(task, lang),
        media=_media_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        executor_header=executor_header(task),
        executor_notice=executor_notice(task, lang),
        tests=_tests_section(task, lang),
    )


_IMPLEMENT_TESTS = {
    "es": "\n4. Ejecuta `{command}` y déjalo pasando antes de terminar.",
    "en": "\n4. Run `{command}` and leave it passing before finishing.",
}

_IMPLEMENT_PROMPT = {
    "es": """Eres el IMPLEMENTADOR de una tarea orquestada por GRAFENO.

# Tarea
- Nombre: {name}
- Proyecto (directorio de trabajo): {workdir}
{remote}{references}{media}
# Tu entrega
1. Lee TODOS los archivos Markdown del directorio de plan, en orden alfabético:
   {plan_dir}
2. Implementa el plan por completo en el proyecto, paso a paso, siguiendo sus
   rutas, comandos y criterios de aceptación.
3. Si un paso es ambiguo, elige la opción más razonable y documéntala en el código.{tests}

{code_rules}

Termina tu respuesta con un resumen de los cambios realizados.

{common_rules}
""",
    "en": """You are the IMPLEMENTER of a task orchestrated by GRAFENO.

# Task
- Name: {name}
- Project (working directory): {workdir}
{remote}{references}{media}
# Your deliverable
1. Read ALL the Markdown files of the plan directory, in alphabetical order:
   {plan_dir}
2. Implement the plan completely in the project, step by step, following its
   paths, commands and acceptance criteria.
3. If a step is ambiguous, choose the most reasonable option and document it in the code.{tests}

{code_rules}

End your answer with a summary of the changes made.

{common_rules}
""",
}


def implement_prompt(task: Task) -> str:
    lang = prompt_language()
    tests = (
        prompt_template(_IMPLEMENT_TESTS, lang).format(command=task.test_command)
        if task.test_command
        else ""
    )
    return _render(
        _IMPLEMENT_PROMPT,
        lang,
        **_fields(task, lang),
        remote=_remote_section(task, lang),
        references=_references_section(task, lang),
        media=_media_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        tests=tests,
    )


_REVIEW_TESTS = {
    "es": "\n- Ejecuta el comando de tests `{command}` y exige que pase.",
    "en": "\n- Run the test command `{command}` and require it to pass.",
}
_REVIEW_TESTS_PASS = {"es": " y los tests pasan", "en": " and the tests pass"}

_REVIEW_PROMPT = {
    "es": """Eres el REVISOR de una tarea orquestada por GRAFENO. Eres estricto pero justo.

# Contexto
- Tarea: {name}
- Proyecto (directorio de trabajo): {workdir}
- Plan que debía implementarse: {plan_dir} (léelo completo, en orden)
{remote}

# Tu entrega
1. Inspecciona los cambios realizados en el proyecto (git status / git diff) y
   el estado final del código.
2. Verifica CADA criterio de aceptación de los archivos del plan.{tests}
3. Además de los problemas, actúa como un revisor senior constructivo: si
   detectas métodos o funciones demasiado complejos, acoplados o difíciles de
   mantener, incluye SUGERENCIAS concretas de mejora (descomposición,
   renombrado, simplificación) que el implementador pueda aplicar. Las
   sugerencias no bloquean la aprobación por sí solas, pero sí los problemas.
4. Escribe tu revisión en el archivo:
   {review_path}
   con secciones: Resumen, Criterios verificados, Problemas encontrados
   (numerados y accionables), Sugerencias de mejora, Recomendaciones.
5. NO modifiques el código del proyecto: solo revisas.
6. TERMINA tu respuesta con una línea EXACTA, sin nada después:
   - `VERDICT: APPROVED` si el plan está cumplido{tests_pass}.
   - `VERDICT: CHANGES_REQUESTED` si falta algo (los problemas numerados del
     archivo de revisión los corregirá el implementador).

{md_rules}

{common_rules}
""",
    "en": """You are the REVIEWER of a task orchestrated by GRAFENO. You are strict but fair.

# Context
- Task: {name}
- Project (working directory): {workdir}
- Plan that had to be implemented: {plan_dir} (read it in full, in order)
{remote}

# Your deliverable
1. Inspect the changes made in the project (git status / git diff) and
   the final state of the code.
2. Verify EACH acceptance criterion of the plan files.{tests}
3. Besides the problems, act as a constructive senior reviewer: if you
   detect methods or functions that are too complex, coupled or hard to
   maintain, include concrete improvement SUGGESTIONS (decomposition,
   renaming, simplification) that the implementer can apply. The
   suggestions do not block approval by themselves, but the problems do.
4. Write your review to the file:
   {review_path}
   with sections: Summary, Verified criteria, Problems found
   (numbered and actionable), Improvement suggestions, Recommendations.
5. Do NOT modify the project code: you only review.
6. END your answer with an EXACT line, with nothing after it:
   - `VERDICT: APPROVED` if the plan is fulfilled{tests_pass}.
   - `VERDICT: CHANGES_REQUESTED` if something is missing (the numbered
     problems of the review file will be fixed by the implementer).

{md_rules}

{common_rules}
""",
}


def review_prompt(task: Task, review_number: int) -> str:
    lang = prompt_language()
    tests = (
        prompt_template(_REVIEW_TESTS, lang).format(command=task.test_command)
        if task.test_command
        else ""
    )
    return _render(
        _REVIEW_PROMPT,
        lang,
        **_fields(task, lang),
        remote=_remote_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        review_path=paths.review_dir(task.id, task.cycle) / f"{review_number:02d}-review.md",
        tests=tests,
        tests_pass=_REVIEW_TESTS_PASS[lang] if task.test_command else "",
    )


_FIX_TESTS = {
    "es": "\n4. Ejecuta `{command}` y déjalo pasando.",
    "en": "\n4. Run `{command}` and leave it passing.",
}

_FIX_PROMPT = {
    "es": """Eres el IMPLEMENTADOR de una tarea orquestada por GRAFENO. El REVISOR
ha pedido correcciones sobre tu trabajo anterior.

# Contexto
- Tarea: {name}
- Proyecto (directorio de trabajo): {workdir}
- Plan original: {plan_dir}
- Revisión con las correcciones pedidas: {review_path}
{remote}

# Tu entrega
1. Lee la revisión completa.
2. Corrige TODOS los problemas numerados, en orden, sin romper lo ya aprobado
   ni desviarte del plan original.
3. Si alguna corrección contradice el plan, prioriza el plan y justifícalo con
   un comentario en el código.{tests}

{code_rules}

Termina tu respuesta con un resumen de las correcciones aplicadas.

{common_rules}
""",
    "en": """You are the IMPLEMENTER of a task orchestrated by GRAFENO. The REVIEWER
has requested corrections on your previous work.

# Context
- Task: {name}
- Project (working directory): {workdir}
- Original plan: {plan_dir}
- Review with the requested corrections: {review_path}
{remote}

# Your deliverable
1. Read the full review.
2. Fix ALL the numbered problems, in order, without breaking what was already
   approved or deviating from the original plan.
3. If a correction contradicts the plan, prioritize the plan and justify it with
   a comment in the code.{tests}

{code_rules}

End your answer with a summary of the corrections applied.

{common_rules}
""",
}


def fix_prompt(task: Task, review_number: int) -> str:
    lang = prompt_language()
    tests = (
        prompt_template(_FIX_TESTS, lang).format(command=task.test_command)
        if task.test_command
        else ""
    )
    return _render(
        _FIX_PROMPT,
        lang,
        **_fields(task, lang),
        remote=_remote_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        review_path=paths.review_dir(task.id, task.cycle) / f"{review_number:02d}-review.md",
        tests=tests,
    )


_FINAL_TESTS = {
    "es": "\n- Ejecuta el comando de tests `{command}` al final y exige que pase.",
    "en": "\n- Run the test command `{command}` at the end and require it to pass.",
}

_FINAL_PROMPT = {
    "es": """Eres el AGENTE DE PASOS FINALES de una tarea orquestada por GRAFENO.
La tarea ya fue implementada y APROBADA por el revisor. Tu trabajo es el cierre.

# Contexto
- Tarea: {name}
- Descripción: {description}
- Proyecto (directorio de trabajo): {workdir}
- Plan implementado: {plan_dir}
- Revisiones del ciclo: {review_dir} (la última aprobó el trabajo)
{remote}{custom_final}
# Tu entrega
1. Inspecciona el estado final del proyecto (git status / git diff).
2. Actualiza la documentación afectada por los cambios (README, AGENTS.md u otros
   documentos del proyecto) si la implementación modificó comportamiento, comandos
   o estructura. Si no hay nada que actualizar, indícalo en el informe.
3. Limpieza final: elimina código muerto, archivos temporales o restos de depuración
   introducidos durante la implementación, SIN alterar el comportamiento aprobado.{tests}
4. Escribe tu informe en el archivo:
   {final_file}
   con secciones: Resumen, Acciones realizadas, Documentación actualizada, Observaciones.
5. NO generes ningún archivo `changes.md`: el sistema lo añade automáticamente
   al directorio del informe con el diff completo de la tarea.

{code_rules}

{md_rules}

Termina tu respuesta con un resumen de las acciones de cierre realizadas.

{common_rules}
""",
    "en": """You are the FINAL-STEPS AGENT of a task orchestrated by GRAFENO.
The task was already implemented and APPROVED by the reviewer. Your job is the wrap-up.

# Context
- Task: {name}
- Description: {description}
- Project (working directory): {workdir}
- Implemented plan: {plan_dir}
- Reviews of the cycle: {review_dir} (the last one approved the work)
{remote}{custom_final}
# Your deliverable
1. Inspect the final state of the project (git status / git diff).
2. Update the documentation affected by the changes (README, AGENTS.md or other
   project documents) if the implementation changed behavior, commands
   or structure. If there is nothing to update, say so in the report.
3. Final cleanup: remove dead code, temporary files or debugging leftovers
   introduced during the implementation, WITHOUT altering the approved behavior.{tests}
4. Write your report to the file:
   {final_file}
   with sections: Summary, Actions taken, Updated documentation, Observations.
5. Do NOT generate any `changes.md` file: the system adds it automatically
   to the report directory with the full diff of the task.

{code_rules}

{md_rules}

End your answer with a summary of the wrap-up actions taken.

{common_rules}
""",
}


def final_prompt(task: Task) -> str:
    lang = prompt_language()
    tests = (
        prompt_template(_FINAL_TESTS, lang).format(command=task.test_command)
        if task.test_command
        else ""
    )
    return _render(
        _FINAL_PROMPT,
        lang,
        **_fields(task, lang),
        remote=_remote_section(task, lang),
        custom_final=_custom_final_section(task, lang),
        plan_dir=paths.plan_dir(task.id, task.cycle),
        review_dir=paths.review_dir(task.id, task.cycle),
        final_file=paths.final_dir(task.id, task.cycle) / "01-final.md",
        tests=tests,
    )
