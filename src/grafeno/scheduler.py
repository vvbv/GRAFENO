"""Lógica de planificación horaria, encadenamiento y repetición de tareas.

Lógica pura (sin TUI, sin asyncio) que decide qué tareas están pendientes de
arrancar y cómo ordenarlas en árbol. Usada por el tick de la App y por el
listado de tareas.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .models import Task, TaskState

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"  # formato aceptado en el formulario


def parse_schedule(text: str) -> str:
    """Valida ``"YYYY-MM-DD HH:MM"`` y devuelve ISO local ``"YYYY-MM-DDTHH:MM"``.

    Cadena vacía devuelve ``""`` (sin programación). Acepta también el
    separador ``"T"``. Lanza ``ValueError`` si el formato no es válido.
    """
    cleaned = text.strip()
    if not cleaned:
        return ""
    candidate = cleaned.replace("T", " ")
    try:
        dt = datetime.strptime(candidate, SCHEDULE_FORMAT)
    except ValueError as exc:
        raise ValueError(
            f"Fecha/hora no válida ({text!r}); usa el formato YYYY-MM-DD HH:MM"
        ) from exc
    return dt.isoformat(timespec="minutes")


def _parse_completed_at(value: str) -> datetime | None:
    """Parsea ``last_completed_at``; si falla, devuelve ``None``."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_due(task: Task, now: datetime) -> bool:
    """True si la tarea debe arrancar YA de forma desatendida.

    Condiciones (todas):
    - estado DRAFT (nunca PAUSED: una pausa es decisión del usuario);
    - si tiene padre, el padre debe estar DONE (se resuelve fuera: ver
      ``parent_done``);
    - si tiene ``scheduled_at``, debe ser pasado o presente;
    - si es repetitiva por intervalo y ya se completó alguna vez, debe haber
      pasado ``repeat_interval_minutes`` desde ``last_completed_at``.
    """
    if task.state is not TaskState.DRAFT:
        return False

    # Repetición por intervalo: la referencia es last_completed_at + intervalo.
    if task.repeat_mode == "interval":
        last = _parse_completed_at(task.last_completed_at)
        if last is None:
            # Nunca completada: cae al scheduled_at. Si no hay, no es due
            # (evita arrancar en bucle nada más crearla).
            if not task.scheduled_at:
                return False
            try:
                target = datetime.fromisoformat(task.scheduled_at)
            except ValueError:
                return False
            return target <= now
        target = last + timedelta(minutes=task.repeat_interval_minutes)
        return target <= now

    # Modo infinito o sin repetición: la hora programada manda.
    if not task.scheduled_at:
        return False
    try:
        target = datetime.fromisoformat(task.scheduled_at)
    except ValueError:
        return False
    return target <= now


def parent_done(task: Task, by_id: dict[str, Task]) -> bool:
    """True si no tiene padre o el padre existe y está DONE."""
    if not task.parent_id:
        return True
    parent = by_id.get(task.parent_id)
    return parent is not None and parent.state is TaskState.DONE


def chain_completed(task: Task, by_id: dict[str, Task]) -> bool:
    """True si la tarea está DONE y TODAS sus descendientes están DONE.

    Se usa para el modo ``"infinite"``: la repetición arranca cuando termina la
    última tarea de la cadena. Devuelve ``False`` si alguna descendiente está
    FAILED o DISCARDED (la cadena rota no reinicia sola).
    """
    if task.state is not TaskState.DONE:
        return False
    visited: set[str] = set()

    def visit(node: Task) -> bool:
        if node.id in visited:
            return True
        visited.add(node.id)
        for candidate in by_id.values():
            if candidate.parent_id == node.id:
                if candidate.state is not TaskState.DONE:
                    return False
                if not visit(candidate):
                    return False
        return True

    return visit(task)


def children(tasks: list[Task], task_id: str) -> list[Task]:
    """Hijas directas de una tarea, conservando el orden de la lista."""
    return [task for task in tasks if task.parent_id == task_id]


def tree_order(tasks: list[Task]) -> list[tuple[Task, int]]:
    """``(tarea, profundidad)`` con cada hija justo tras su padre.

    La lista de entrada ya viene ordenada (``list_all``: más reciente primero).
    Las tareas cuyo padre no esté en la lista (filtrado u otro proyecto) se
    muestran como raíz con profundidad 0. Es inmune a ciclos de ``parent_id``.
    """
    by_parent: dict[str, list[Task]] = {}
    for task in tasks:
        by_parent.setdefault(task.parent_id, []).append(task)
    ids = {task.id for task in tasks}

    roots = [task for task in tasks if task.parent_id not in ids]
    result: list[tuple[Task, int]] = []
    visited: set[str] = set()

    def visit(task: Task, depth: int) -> None:
        if task.id in visited:
            return
        visited.add(task.id)
        result.append((task, depth))
        for child in by_parent.get(task.id, []):
            visit(child, depth + 1)

    for root in roots:
        visit(root, 0)
    return result


def prepare_next_iteration(task: Task) -> None:
    """Reinicia la máquina de estados para la siguiente repetición.

    Deja ``state=DRAFT``, ``iteration=0``, ``cycle=1``, ``sessions={}``. NO
    toca los archivos de plan: eso lo decide el llamador según ``plan_reuse``.
    El campo ``repeat_count`` lo incrementa el llamador (no esta función).
    """
    task.state = TaskState.DRAFT
    task.iteration = 0
    task.cycle = 1
    task.sessions = {}
