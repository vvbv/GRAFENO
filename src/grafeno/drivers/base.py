"""Abstracción de CLIs de agentes de programación.

Un ``CLIDriver`` sabe construir el comando no-interactivo de su CLI,
interpretar sus eventos de salida (JSONL), listar sus modelos y construir
el prompt de generación de AGENTS.md. El orquestador solo habla con esta
interfaz, por lo que añadir un CLI nuevo (Codex, Claude Code, …) implica
crear un único archivo en ``drivers/`` y registrarlo en
``drivers/__init__.py``.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable

from ..i18n import t


_READ_CHUNK = 65536  # bytes read per stream.read() call


async def read_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """Yield decoded text lines from a stream without any line-length limit.

    Uses chunked ``stream.read()`` calls plus an incremental UTF-8 decoder
    rather than the stream's built-in line helper, which raises
    ``ValueError`` ("Separator is found, but chunk is longer than limit")
    when a single line exceeds the asyncio stream limit (64 KiB by default).
    Agent CLIs can emit JSONL events well above that size.
    """
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer


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
    # Comando nativo del CLI para generar AGENTS.md (p.ej. "/init").
    # Vacío si el CLI no tiene uno: se usa el prompt genérico.
    init_command: str = ""

    # ------------------------------------------------------------ #
    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def build_command(self, request: RunRequest) -> list[str]:
        raise NotImplementedError

    def models_command(self) -> list[str]:
        """Comando del CLI que lista los modelos disponibles."""
        raise NotImplementedError

    def parse_models(self, output: str) -> list[str]:
        """Interpreta la salida de ``models_command`` y devuelve los modelos."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Versión síncrona (bloqueante): para uso fuera de la TUI."""
        output = self._run_sync(self.models_command())
        return self.parse_models(output) if output else []

    async def list_models_async(self, timeout: float = 30.0) -> list[str]:
        """Versión asíncrona y cancelable: al cancelar se mata el subproceso."""
        try:
            process = await asyncio.create_subprocess_exec(
                *self.models_command(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return []
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return []
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            return []
        return self.parse_models(stdout.decode("utf-8", errors="replace"))

    def build_agents_md_prompt(self) -> str:
        """Construye el prompt para generar el AGENTS.md del proyecto.

        Si el CLI tiene un comando nativo de inicialización (``init_command``),
        el prompt pide ejecutar su equivalente; si no, pide el análisis manual.
        """
        if self.init_command:
            instruccion = (
                f"Este CLI dispone del comando `{self.init_command}` exactamente "
                "para esto: ejecútalo si está disponible en este modo; si no lo "
                "está, realiza tú mismo el mismo análisis y escribe el archivo "
                "siguiendo las convenciones habituales de ese comando."
            )
        else:
            instruccion = (
                "Realiza un análisis del repositorio y escribe el archivo "
                "siguiendo las convenciones habituales de los comandos `/init` "
                "de los agentes de programación."
            )
        return f"""Analiza este repositorio y crea un archivo AGENTS.md en su raíz.

{instruccion}

El AGENTS.md debe ser conciso y útil para un agente de programación:
- estructura del proyecto y propósito de cada parte;
- stack y dependencias;
- cómo compilar/ejecutar y cómo lanzar los tests;
- convenciones de estilo y de commits que ya se observen en el código.

Reglas:
- Escribe SOLO el archivo AGENTS.md en la raíz del repositorio; no modifiques
  ningún otro archivo.
- Nada de emojis.
- Termina tu respuesta con una línea que indique la ruta del archivo creado.
"""

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
            return RunResult(ok=False, error=t("drv.exec_error", command=command[0], error=exc))

        async def pump_stdout() -> None:
            nonlocal session_id
            assert process.stdout is not None
            async for line in read_lines(process.stdout):
                line = line.rstrip("\r")
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
            async for line in read_lines(process.stderr):
                stderr_parts.append(line.rstrip("\r"))

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
            error = t("drv.exit_error", name=self.display_name, code=returncode)
            if tail:
                error += f"\n{tail}"
        return RunResult(
            ok=ok,
            text="\n".join(part for part in text_parts if part).strip(),
            session_id=session_id,
            error=error,
            returncode=returncode,
        )
