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


def plan_prompt(task: Task) -> str:
    plan_dir = paths.plan_dir(task.id)
    return f"""Eres el PLANIFICADOR de una tarea de programación orquestada por GRAFENO.

# Tarea
- Nombre: {task.name}
- Descripción: {task.description or "(sin descripción)"}
- Proyecto (directorio de trabajo): {task.workdir}

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
5. NO implementes el código: solo planifica.
6. Termina tu respuesta con un resumen de 3 líneas y la lista de archivos escritos.

{_COMMON_RULES}
""".strip()


def implement_prompt(task: Task) -> str:
    plan_dir = paths.plan_dir(task.id)
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

Termina tu respuesta con un resumen de los cambios realizados.

{_COMMON_RULES}
""".strip()


def review_prompt(task: Task, review_number: int) -> str:
    plan_dir = paths.plan_dir(task.id)
    review_path = paths.review_dir(task.id) / f"{review_number:02d}-review.md"
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
3. Escribe tu revisión en el archivo:
   {review_path}
   con secciones: Resumen, Criterios verificados, Problemas encontrados
   (numerados y accionables), Recomendaciones.
4. NO modifiques el código del proyecto: solo revisas.
5. TERMINA tu respuesta con una línea EXACTA, sin nada después:
   - `VERDICT: APPROVED` si el plan está cumplido{((" y los tests pasan") if task.test_command else "")}.
   - `VERDICT: CHANGES_REQUESTED` si falta algo (los problemas numerados del
     archivo de revisión los corregirá el implementador).

{_COMMON_RULES}
""".strip()


def fix_prompt(task: Task, review_number: int) -> str:
    review_path = paths.review_dir(task.id) / f"{review_number:02d}-review.md"
    plan_dir = paths.plan_dir(task.id)
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

Termina tu respuesta con un resumen de las correcciones aplicadas.

{_COMMON_RULES}
""".strip()
