"""Abstracción de CLIs de agentes de programación.

Un ``CLIDriver`` sabe construir el comando no-interactivo de su CLI,
interpretar sus eventos de salida (JSONL) y listar sus modelos. El
orquestador solo habla con esta interfaz, por lo que añadir un CLI nuevo
(Codex, Claude Code, …) implica crear un único archivo en ``drivers/`` y
registrarlo en ``drivers/__init__.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable


class EventKind(Enum):
    TEXT = "text"    # texto del asistente
    TOOL = "tool"    # uso de herramienta (lectura/escritura/comandos)
    INFO = "info"    # mensajes internos de estado
    ERROR = "error"  # errores reportados por el CLI


@dataclass
class RunEvent:
    kind: EventKind
    text: str


@dataclass
class RunRequest:
    prompt: str
    model: str  # vacío = modelo por defecto del CLI
    workdir: Path
    session_id: str | None = None  # continuar sesión previa (mejor esfuerzo)
    log_path: Path | None = None   # dónde volcar la salida cruda
    title: str = ""


@dataclass
class RunResult:
    ok: bool
    text: str = ""                 # texto agregado del asistente
    session_id: str | None = None
    error: str = ""
    returncode: int | None = None


EventCallback = Callable[[RunEvent], None]


class CLIDriver:
    """Clase base: implementa el ciclo de subproceso; las subclases el dialecto."""

    name: str = ""
    display_name: str = ""
    executable: str = ""

    # ------------------------------------------------------------ #
    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def build_command(self, request: RunRequest) -> list[str]:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError

    def decode_line(self, line: str) -> tuple[RunEvent | None, str | None]:
        """Interpreta una línea de salida. Devuelve (evento, session_id|None)."""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            stripped = line.rstrip()
            return (RunEvent(EventKind.TEXT, stripped) if stripped else None), None
        return self.decode_event(payload)

    def decode_event(self, payload: dict) -> tuple[RunEvent | None, str | None]:
        """Interpreta un evento JSON ya parseado (dialecto del CLI)."""
        raise NotImplementedError

    # ------------------------------------------------------------ #
    def _run_sync(self, command: list[str]) -> str | None:
        """Ejecuta un comando auxiliar corto (p.ej. listar modelos)."""
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None

    async def run(
        self,
        request: RunRequest,
        on_event: EventCallback | None = None,
        on_activity: Callable[[], None] | None = None,
    ) -> RunResult:
        command = self.build_command(request)
        log_handle = request.log_path.open("a", encoding="utf-8") if request.log_path else None
        text_parts: list[str] = []
        session_id: str | None = None
        stderr_parts: list[str] = []

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(request.workdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            if log_handle:
                log_handle.close()
            return RunResult(ok=False, error=f"No se pudo ejecutar {command[0]}: {exc}")

        async def pump_stdout() -> None:
            nonlocal session_id
            assert process.stdout is not None
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if log_handle:
                    log_handle.write(line + "\n")
                    log_handle.flush()
                if on_activity:
                    on_activity()  # latido: el CLI sigue emitiendo salida
                event, found_session = self.decode_line(line)
                if found_session:
                    session_id = found_session
                if event is None:
                    continue
                if event.kind is EventKind.TEXT:
                    text_parts.append(event.text)
                if on_event:
                    on_event(event)

        async def pump_stderr() -> None:
            assert process.stderr is not None
            while True:
                raw = await process.stderr.readline()
                if not raw:
                    break
                stderr_parts.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

        try:
            await asyncio.gather(pump_stdout(), pump_stderr())
            returncode = await process.wait()
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        finally:
            if log_handle:
                log_handle.close()

        ok = returncode == 0
        error = ""
        if not ok:
            tail = "\n".join(stderr_parts[-10:]).strip()
            error = f"{self.display_name} terminó con código {returncode}."
            if tail:
                error += f"\n{tail}"
        return RunResult(
            ok=ok,
            text="\n".join(part for part in text_parts if part).strip(),
            session_id=session_id,
            error=error,
            returncode=returncode,
        )
