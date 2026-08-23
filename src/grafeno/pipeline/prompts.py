"""Plantillas de prompts para cada fase del pipeline de GRAFENO.

Todos los planes incluyen una cabecera ``GRAFENO-EXECUTOR`` que declara qué
modelo y qué CLI los implementará, y exigen al planificador optimizar el
contenido para ese ejecutor (modos de configuración 1 y 2).
"""

from __future__ import annotations

from ..models import Task
from .. import paths

EXECUTOR_HEADER_TEMPLATE = """<!-- GRAFENO-EXECUTOR
cli: {cli}
model: {model}
-->"""

EXECUTOR_NOTICE_TEMPLATE = (
    "> **Ejecutor de este plan**: lo implementará el modelo `{model}` "
    "a través del CLI `{cli}`. Este plan está optimizado para ese ejecutor."
)

_COMMON_RULES = """
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
""".strip()

_CODE_RULES = """
Reglas de código (obligatorias al implementar):
- Nada de emotes/emojis en el código, los comentarios ni la documentación.
- La documentación, los comentarios y los nombres de métodos/funciones/clases
  se escriben en INGLÉS, salvo que el proyecto tenga de forma considerable
  otro idioma dominante: en ese caso, sigue el estilo de codificación ya
  existente en el proyecto.
""".strip()


def executor_header(task: Task) -> str:
    return EXECUTOR_HEADER_TEMPLATE.format(cli=task.implementer.cli, model=task.implementer.model or "default")


def executor_notice(task: Task) -> str:
    return EXECUTOR_NOTICE_TEMPLATE.format(
        cli=task.implementer.cli, model=task.implementer.model or "default"
    )


def _tests_section(task: Task) -> str:
    if not task.test_command:
        return ""
    return (
        f"\n- La tarea define un comando de tests: `{task.test_command}`. "
        "Incluye en el plan cómo satisfacerlo."
    )


def _cycle_section(task: Task) -> str:
    """Contexto de ampliación cuando la tarea está en un ciclo ≥2."""
    if task.cycle <= 1:
        return ""
    return f"""
# Ampliación (ciclo {task.cycle})
Esta tarea ya completó ciclos anteriores: el proyecto contiene ese trabajo.
Nueva petición del usuario sobre ese trabajo:
{task.current_extension or "(sin detalle)"}

Planifica SOLO esta ampliación: no repitas lo ya implementado.
"""


def _custom_final_section(task: Task) -> str:
    """Instrucciones extra del usuario para la fase final (vacías = sin sección)."""
    if not task.final_prompt.strip():
        return ""
    return f"""
# Instrucciones adicionales del usuario para el cierre
{task.final_prompt.strip()}
"""


def plan_prompt(task: Task) -> str:
    plan_dir = paths.plan_dir(task.id, task.cycle)
    return f"""Eres un INGENIERO DE SOFTWARE SENIOR actuando como PLANIFICADOR de una
tarea de programación orquestada por GRAFENO.

# Tarea
- Nombre: {task.name}
- Descripción: {task.description or "(sin descripción)"}
- Proyecto (directorio de trabajo): {task.workdir}
{_cycle_section(task)}
# Tu entrega
1. Explora el proyecto para entender su estructura, stack y convenciones.
2. Escribe el plan en UNO O VARIOS archivos Markdown dentro de:
   {plan_dir}
   Nómbralos `NN-slug.md` (p.ej. `01-setup.md`, `02-api.md`) en orden de ejecución.
3. CADA archivo debe comenzar EXACTAMENTE con esta cabecera (sin modificar):

{executor_header(task)}
{executor_notice(task)}

4. El plan lo ejecutará OTRO modelo (`{task.implementer.model or "default"}` vía CLI `{task.implementer.cli}`),
   que no compartirá tu contexto. OPTIMIZA el plan para que ese ejecutor lo
   implemente sin ambigüedades:
   - pasos pequeños, numerados y verificables;
   - rutas de archivos exactas a crear o modificar;
   - comandos concretos listos para copiar;
   - fragmentos de código clave cuando aporten claridad;
   - criterios de aceptación explícitos al final de cada archivo.{_tests_section(task)}
5. Para cualquier método o función que consideres COMPLEJO para el modelo que
   implementará (`{task.implementer.model or "default"}` vía `{task.implementer.cli}`),
   añade un bloque "Sugerencias" junto a ese paso con:
   - descomposición del método en funciones más pequeñas, si aplica;
   - pseudocódigo o la firma exacta del método;
   - alternativas más simples de implementar y advertencias de errores típicos.
6. NO implementes el código: solo planifica.
7. El plan debe incluir literalmente estas reglas para el ejecutor:

{_CODE_RULES}

8. Termina tu respuesta con un resumen de 3 líneas y la lista de archivos escritos.

{_COMMON_RULES}
""".strip()


def reevaluate_plan_prompt(task: Task) -> str:
    """Prompt de reevaluación: ajusta el plan existente según la descripción."""
    plan_dir = paths.plan_dir(task.id, task.cycle)
    return f"""Eres un INGENIERO DE SOFTWARE SENIOR actuando como PLANIFICADOR de una
tarea de programación orquestada por GRAFENO. Esta es una REEVALUACIÓN de un
plan existente, NO una planificación desde cero.

# Tarea
- Nombre: {task.name}
- Descripción: {task.description or "(sin descripción)"}
- Proyecto (directorio de trabajo): {task.workdir}
{_cycle_section(task)}
# Tu entrega
1. La tarea YA TIENE archivos de plan en:
   {plan_dir}
   LEE PRIMERO esos archivos (en orden alfabético) para entender qué se
   planificó en repeticiones anteriores.
2. Compara el plan existente con la descripción ORIGINAL de la tarea (arriba)
   y con el estado actual del proyecto en `{task.workdir}`.
3. ACTUALIZA los archivos del plan solo donde proceda:
   - mantén el formato de cabecera EXACTO en cada archivo (sin modificar):

{executor_header(task)}
{executor_notice(task)}

   - ajusta pasos, criterios de aceptación y sugerencias a la realidad del
     proyecto;
   - añade o elimina archivos `NN-slug.md` si la estructura del plan cambia;
   - mantén el orden de numeración coherente.
4. Si el plan sigue siendo válido, NO lo modifiques: indícalo en tu respuesta
   y no escribas archivos nuevos.
5. El plan lo ejecutará OTRO modelo (`{task.implementer.model or "default"}` vía CLI `{task.implementer.cli}`),
   que no compartirá tu contexto. OPTIMIZA el plan para ese ejecutor.{_tests_section(task)}
6. El plan debe incluir literalmente estas reglas para el ejecutor:

{_CODE_RULES}

7. Termina tu respuesta con un resumen de los cambios (o de "sin cambios").

{_COMMON_RULES}
""".strip()


def implement_prompt(task: Task) -> str:
    plan_dir = paths.plan_dir(task.id, task.cycle)
    tests = (
        f"\n4. Ejecuta `{task.test_command}` y déjalo pasando antes de terminar."
        if task.test_command
        else ""
    )
    return f"""Eres el IMPLEMENTADOR de una tarea orquestada por GRAFENO.

# Tarea
- Nombre: {task.name}
- Proyecto (directorio de trabajo): {task.workdir}

# Tu entrega
1. Lee TODOS los archivos Markdown del directorio de plan, en orden alfabético:
   {plan_dir}
2. Implementa el plan por completo en el proyecto, paso a paso, siguiendo sus
   rutas, comandos y criterios de aceptación.
3. Si un paso es ambiguo, elige la opción más razonable y documéntala en el código.{tests}

{_CODE_RULES}

Termina tu respuesta con un resumen de los cambios realizados.

{_COMMON_RULES}
""".strip()


def review_prompt(task: Task, review_number: int) -> str:
    plan_dir = paths.plan_dir(task.id, task.cycle)
    review_path = paths.review_dir(task.id, task.cycle) / f"{review_number:02d}-review.md"
    tests = (
        f"\n- Ejecuta el comando de tests `{task.test_command}` y exige que pase."
        if task.test_command
        else ""
    )
    return f"""Eres el REVISOR de una tarea orquestada por GRAFENO. Eres estricto pero justo.

# Contexto
- Tarea: {task.name}
- Proyecto (directorio de trabajo): {task.workdir}
- Plan que debía implementarse: {plan_dir} (léelo completo, en orden)

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
   - `VERDICT: APPROVED` si el plan está cumplido{((" y los tests pasan") if task.test_command else "")}.
   - `VERDICT: CHANGES_REQUESTED` si falta algo (los problemas numerados del
     archivo de revisión los corregirá el implementador).

{_COMMON_RULES}
""".strip()


def fix_prompt(task: Task, review_number: int) -> str:
    review_path = paths.review_dir(task.id, task.cycle) / f"{review_number:02d}-review.md"
    plan_dir = paths.plan_dir(task.id, task.cycle)
    tests = (
        f"\n4. Ejecuta `{task.test_command}` y déjalo pasando."
        if task.test_command
        else ""
    )
    return f"""Eres el IMPLEMENTADOR de una tarea orquestada por GRAFENO. El REVISOR
ha pedido correcciones sobre tu trabajo anterior.

# Contexto
- Tarea: {task.name}
- Proyecto (directorio de trabajo): {task.workdir}
- Plan original: {plan_dir}
- Revisión con las correcciones pedidas: {review_path}

# Tu entrega
1. Lee la revisión completa.
2. Corrige TODOS los problemas numerados, en orden, sin romper lo ya aprobado
   ni desviarte del plan original.
3. Si alguna corrección contradice el plan, prioriza el plan y justifícalo con
   un comentario en el código.{tests}

{_CODE_RULES}

Termina tu respuesta con un resumen de las correcciones aplicadas.

{_COMMON_RULES}
""".strip()


def final_prompt(task: Task) -> str:
    plan_dir = paths.plan_dir(task.id, task.cycle)
    review_dir = paths.review_dir(task.id, task.cycle)
    final_dir = paths.final_dir(task.id, task.cycle)
    tests = (
        f"\n- Ejecuta el comando de tests `{task.test_command}` al final y exige que pase."
        if task.test_command
        else ""
    )
    return f"""Eres el AGENTE DE PASOS FINALES de una tarea orquestada por GRAFENO.
La tarea ya fue implementada y APROBADA por el revisor. Tu trabajo es el cierre.

# Contexto
- Tarea: {task.name}
- Descripción: {task.description or "(sin descripción)"}
- Proyecto (directorio de trabajo): {task.workdir}
- Plan implementado: {plan_dir}
- Revisiones del ciclo: {review_dir} (la última aprobó el trabajo)
{_custom_final_section(task)}
# Tu entrega
1. Inspecciona el estado final del proyecto (git status / git diff).
2. Actualiza la documentación afectada por los cambios (README, AGENTS.md u otros
   documentos del proyecto) si la implementación modificó comportamiento, comandos
   o estructura. Si no hay nada que actualizar, indícalo en el informe.
3. Limpieza final: elimina código muerto, archivos temporales o restos de depuración
   introducidos durante la implementación, SIN alterar el comportamiento aprobado.{tests}
4. Escribe tu informe en el archivo:
   {final_dir / "01-final.md"}
   con secciones: Resumen, Acciones realizadas, Documentación actualizada, Observaciones.

{_CODE_RULES}

Termina tu respuesta con un resumen de las acciones de cierre realizadas.

{_COMMON_RULES}
""".strip()
