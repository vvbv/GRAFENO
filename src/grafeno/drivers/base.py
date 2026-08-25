"""Abstraction for coding-agent CLIs.

A ``CLIDriver`` knows how to build the non-interactive command of its CLI,
interpret its output events (JSONL), list its models and build the prompt
that generates AGENTS.md. Each driver also exposes its native self-update
command (``update_command()``) so the TUI can refresh installed CLIs on
startup when the user enables it. The orchestrator only talks to this
interface, so adding a new CLI (e.g. in the future) means creating a
single file in ``drivers/`` and registering it in ``drivers/__init__.py``.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable

from .. import ratelimit
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
    TEXT = "text"    # assistant text
    TOOL = "tool"    # tool usage (read/write/commands)
    INFO = "info"    # internal status messages
    ERROR = "error"  # errors reported by the CLI


@dataclass
class RunEvent:
    kind: EventKind
    text: str


@dataclass
class TokenUsage:
    """Tokens consumed by a run (or by a single event)."""

    input: int = 0
    output: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input += other.input
        self.output += other.output

    @property
    def empty(self) -> bool:
        return self.input == 0 and self.output == 0


@dataclass
class RunRequest:
    prompt: str
    model: str  # empty = CLI default model
    workdir: Path
    session_id: str | None = None  # continue a previous session (best effort)
    log_path: Path | None = None   # where to dump the raw output
    title: str = ""
    effort: str = ""               # model effort level; empty = CLI default


@dataclass
class RunResult:
    ok: bool
    text: str = ""                 # aggregated assistant text
    session_id: str | None = None
    error: str = ""
    returncode: int | None = None
    tokens: TokenUsage = field(default_factory=TokenUsage)  # aggregated usage
    # Seconds to wait before retrying when the CLI reported usage/quota
    # exhaustion; 0.0 = exhausted without a time hint; None = not a usage error.
    usage_wait: float | None = None


EventCallback = Callable[[RunEvent], None]


class CLIDriver:
    """Base class: implements the subprocess loop; subclasses implement the dialect."""

    name: str = ""
    display_name: str = ""
    executable: str = ""
    # Native CLI command for generating AGENTS.md (e.g. "/init").
    # Empty if the CLI has none: the generic prompt is used instead.
    init_command: str = ""

    # ------------------------------------------------------------ #
    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def build_command(self, request: RunRequest) -> list[str]:
        raise NotImplementedError

    def models_command(self) -> list[str]:
        """CLI command that lists the available models."""
        raise NotImplementedError

    def parse_models(self, output: str) -> list[str]:
        """Interpret the output of ``models_command`` and return the models."""
        raise NotImplementedError

    def list_models(self) -> list[str]:
        """Synchronous (blocking) version: for use outside the TUI."""
        output = self._run_sync(self.models_command())
        return self.parse_models(output) if output else []

    async def list_models_async(self, timeout: float = 30.0) -> list[str]:
        """Async and cancelable version: cancelling kills the subprocess."""
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

    def variants_command(self) -> list[str]:
        """CLI command that lists the effort variants per model.

        Empty = the CLI does not support configurable effort levels.
        """
        return []

    def update_command(self) -> list[str]:
        """Native self-update command of the CLI (e.g. ``claude update``).

        Empty = the CLI has no known self-update command; it is skipped.
        """
        return []

    def parse_variants(self, output: str) -> dict[str, list[str]]:
        """Interpret the output of ``variants_command``.

        Returns ``{model: [levels...]}``; empty if there is no support.
        """
        return {}

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """Async and cancelable version, mirror of ``list_models_async``."""
        command = self.variants_command()
        if not command:
            return {}
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return {}
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return {}
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            return {}
        return self.parse_variants(stdout.decode("utf-8", errors="replace"))

    def build_agents_md_prompt(self) -> str:
        """Build the prompt for generating the project's AGENTS.md.

        If the CLI has a native init command (``init_command``), the prompt
        asks to run its equivalent; otherwise it asks for the manual
        analysis.
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

    def decode_line(self, line: str) -> tuple[RunEvent | None, str | None, TokenUsage | None]:
        """Interpret a line. Returns (event, session_id|None, usage|None)."""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            stripped = line.rstrip()
            return (RunEvent(EventKind.TEXT, stripped) if stripped else None), None, None
        event, session_id = self.decode_event(payload)
        return event, session_id, self.extract_usage(payload)

    def extract_usage(self, payload: dict) -> TokenUsage | None:
        """Extract token usage from an already-parsed JSON event.

        Subclasses override this according to the CLI's dialect.
        ``None`` = the event carries no usage information.
        """
        return None

    def detect_usage_wait(self, text: str) -> float | None:
        """Seconds to wait if ``text`` signals usage/quota exhaustion.

        ``None`` = not a usage-limit error; ``0.0`` = exhausted without a
        time hint (probe periodically); ``> 0`` = explicit wait hint.
        Subclasses may override for CLI-specific formats.
        """
        return ratelimit.detect_usage_wait(text)

    def decode_event(self, payload: dict) -> tuple[RunEvent | None, str | None]:
        """Interpret an already-parsed JSON event (CLI dialect)."""
        raise NotImplementedError

    # ------------------------------------------------------------ #
    def _run_sync(self, command: list[str]) -> str | None:
        """Run a short auxiliary command (e.g. listing models)."""
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
        error_parts: list[str] = []  # texts of ERROR events emitted by the CLI
        tokens = TokenUsage()

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
                    on_activity()  # heartbeat: the CLI is still emitting output
                event, found_session, usage = self.decode_line(line)
                if found_session:
                    session_id = found_session
                if usage:
                    tokens.add(usage)
                if event is None:
                    continue
                if event.kind is EventKind.TEXT:
                    text_parts.append(event.text)
                if event.kind is EventKind.ERROR:
                    error_parts.append(event.text)
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
        usage_wait = self._classify_usage_wait(error, error_parts) if not ok else None
        return RunResult(
            ok=ok,
            text="\n".join(part for part in text_parts if part).strip(),
            session_id=session_id,
            error=error,
            returncode=returncode,
            tokens=tokens,
            usage_wait=usage_wait,
        )

    def _classify_usage_wait(self, error: str, error_parts: list[str]) -> float | None:
        """Combine stderr tail + ERROR events and classify usage exhaustion."""
        combined = "\n".join([error, *error_parts[-20:]])
        return self.detect_usage_wait(combined)
