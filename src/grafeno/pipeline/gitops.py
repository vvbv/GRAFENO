"""Operaciones git del proyecto destino (rama por tarea). Nunca lanza."""

from __future__ import annotations

import subprocess
from pathlib import Path


def _git(workdir: Path, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def is_git_repo(workdir: Path) -> bool:
    ok, output = _git(workdir, "rev-parse", "--is-inside-work-tree")
    return ok and output.strip() == "true"


def create_branch(workdir: Path, branch: str) -> tuple[bool, str]:
    """Crea (o cambia a) la rama de la tarea. Devuelve (éxito, mensaje)."""
    ok, output = _git(workdir, "checkout", "-b", branch)
    if ok:
        return True, f"Rama '{branch}' creada."
    if "already exists" in output:
        ok, output = _git(workdir, "checkout", branch)
        if ok:
            return True, f"Rama '{branch}' ya existía; se ha seleccionado."
    return False, output or f"No se pudo crear la rama '{branch}'."
