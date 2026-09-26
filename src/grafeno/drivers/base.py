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
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import AsyncIterator, Callable

from .. import ratelimit
from ..i18n import prompt_template, t


_READ_CHUNK = 65536  # bytes read per stream.read() call
_STDERR_TAIL_LINES = 10  # stderr lines kept in a failure message
_ERROR_LINE = re.compile(r"^\s*(error|fatal|panic|exception|traceback)\b", re.IGNORECASE)


def stderr_tail(lines: list[str], limit: int = _STDERR_TAIL_LINES) -> str:
    """Diagnostic tail of a failed run's stderr.

    Some CLIs (e.g. kimi) echo tool output to stderr before the actual error,
    so a plain "last N lines" tail fills the failure message with command
    output. When the tail contains an error-looking line, it starts at the
    first one; otherwise the plain last ``limit`` lines are kept.
    """
    tail = [line for line in lines[-limit:] if line.strip()]
    for index, line in enumerate(tail):
        if _ERROR_LINE.match(line):
            tail = tail[index:]
            break
    return "\n".join(tail).strip()


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


def format_error_message(payload: dict, *candidates: object) -> str:
    """Build a readable error message from a JSON event payload.

    Candidates are tried in order: the first non-empty string wins; a dict
    is inspected for a nested message (``message`` or ``data.message``)
    decorated with its ``name``/``ref``; when nothing matches, the payload
    is dumped as JSON (never the Python repr, which leaked single-quoted
    dicts into the live log).
    """
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate:
                return candidate
        elif isinstance(candidate, dict):
            text = _format_error_dict(candidate)
            if text:
                return text
    return json.dumps(payload, ensure_ascii=False)


def _format_error_dict(obj: dict) -> str:
    """Human-readable text out of an error dict like ``{name, data: {message, ref}}``."""
    data = obj.get("data")
    data = data if isinstance(data, dict) else {}
    message = obj.get("message") or data.get("message")
    name = obj.get("name")
    ref = obj.get("ref") or data.get("ref")
    if isinstance(message, str) and message:
        text = f"{name}: {message}" if isinstance(name, str) and name else message
    else:
        text = json.dumps(obj, ensure_ascii=False)
    if isinstance(ref, str) and ref:
        text += f" (ref: {ref})"
    return text


# AGENTS.md generation prompt, per prompt language (``i18n.prompt_template``).
_AGENTS_MD_NATIVE_INIT = {
    "es": (
        "Este CLI dispone del comando `{command}` exactamente "
        "para esto: ejecútalo si está disponible en este modo; si no lo "
        "está, realiza tú mismo el mismo análisis y escribe el archivo "
        "siguiendo las convenciones habituales de ese comando."
    ),
    "en": (
        "This CLI provides the `{command}` command exactly "
        "for this: run it if it is available in this mode; if it is "
        "not, perform the same analysis yourself and write the file "
        "following the usual conventions of that command."
    ),
}
_AGENTS_MD_GENERIC_INIT = {
    "es": (
        "Realiza un análisis del repositorio y escribe el archivo "
        "siguiendo las convenciones habituales de los comandos `/init` "
        "de los agentes de programación."
    ),
    "en": (
        "Analyze the repository and write the file "
        "following the usual conventions of the `/init` commands "
        "of coding agents."
    ),
}
_AGENTS_MD_PROMPT = {
    "es": """Analiza este repositorio y crea un archivo AGENTS.md en su raíz.

{instruction}

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
""",
    "en": """Analyze this repository and create an AGENTS.md file at its root.

{instruction}

The AGENTS.md must be concise and useful for a coding agent:
- project structure and the purpose of each part;
- stack and dependencies;
- how to build/run and how to run the tests;
- style and commit conventions already observed in the code.

Rules:
- Write ONLY the AGENTS.md file at the root of the repository; do not modify
  any other file.
- No emojis.
- End your answer with a line stating the path of the created file.
""",
}


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
    # Cached absolute path of the executable (None = not resolved yet).
    _exe_path: str | None = None

    # ------------------------------------------------------------ #
    def is_available(self) -> bool:
        return shutil.which(self.executable) is not None

    def resolve_executable(self) -> str:
        """Absolute path of the CLI executable, resolved once and cached.

        On Windows the agent CLIs are usually npm shims (``.cmd``/``.ps1``):
        ``shutil.which`` finds them (so ``is_available`` is True) but a bare
        name cannot be spawned (CreateProcess only implies ``.exe``), which
        raised ``OSError`` and silently emptied the model/variant lists.
        Spawning the resolved path fixes listing and task runs on Windows.
        """
        if self._exe_path is None:
            self._exe_path = shutil.which(self.executable) or self.executable
        return self._exe_path

    def resolve_command(self, command: list[str]) -> list[str]:
        """Replace ``command[0]`` with its resolved absolute path."""
        if not command:
            return command
        head = command[0]
        resolved = (
            self.resolve_executable()
            if head == self.executable
            else (shutil.which(head) or head)
        )
        return [resolved, *command[1:]]

    def build_command(self, request: RunRequest) -> list[str]:
        raise NotImplementedError

    def run_env(self, request: RunRequest) -> dict[str, str] | None:
        """Environment for the run subprocess; ``None`` inherits the current one."""
        return None

    def stdin_prompt(self) -> bool:
        """True if ``RunRequest.prompt`` travels via stdin instead of argv.

        On Windows the agent CLIs are npm shims (``.cmd``): ``cmd.exe`` cuts
        a quoted argument at the first newline, so multi-line prompts arrived
        truncated at the CLI. Drivers whose CLI documents reading the prompt
        from stdin (``claude -p``, ``opencode run``, ``codex exec``) override
        this and omit the prompt from ``build_command``; the rest keep the
        prompt in argv (e.g. ``kimi -p``).
        """
        return False

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
        command = self.resolve_command(self.models_command())
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
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
        command = self.resolve_command(self.variants_command())
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
        analysis. Rendered in the configured prompt language.
        """
        if self.init_command:
            instruction = prompt_template(_AGENTS_MD_NATIVE_INIT).format(command=self.init_command)
        else:
            instruction = prompt_template(_AGENTS_MD_GENERIC_INIT)
        return prompt_template(_AGENTS_MD_PROMPT).format(instruction=instruction)

    def decode_line(
        self, line: str
    ) -> tuple[RunEvent | list[RunEvent] | None, str | None, TokenUsage | None]:
        """Interpret a line. Returns (event(s), session_id|None, usage|None).

        A single JSONL line may yield several events (e.g. an assistant
        message mixing text and tool_use blocks): drivers may return a list.
        """
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

    def decode_event(
        self, payload: dict
    ) -> tuple[RunEvent | list[RunEvent] | None, str | None]:
        """Interpret an already-parsed JSON event (CLI dialect)."""
        raise NotImplementedError

    # ------------------------------------------------------------ #
    def _run_sync(self, command: list[str]) -> str | None:
        """Run a short auxiliary command (e.g. listing models)."""
        command = self.resolve_command(command)
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
        command = self.resolve_command(self.build_command(request))
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
                env=self.run_env(request),
                stdin=(asyncio.subprocess.PIPE if self.stdin_prompt() else None),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            if log_handle:
                log_handle.close()
            return RunResult(ok=False, error=t("drv.exec_error", command=command[0], error=exc))

        async def feed_stdin() -> None:
            """Write the prompt to stdin and close it (a broken pipe is fine)."""
            if process.stdin is None:
                return
            try:
                process.stdin.write(request.prompt.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # CLI died before reading: the stderr tail reports it
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

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
                decoded, found_session, usage = self.decode_line(line)
                if found_session:
                    session_id = found_session
                if usage:
                    tokens.add(usage)
                events = decoded if isinstance(decoded, list) else (
                    [decoded] if decoded is not None else []
                )
                for event in events:
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
            await asyncio.gather(pump_stdout(), pump_stderr(), feed_stdin())
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
            tail = stderr_tail(stderr_parts)
            error = t("drv.exit_error", name=self.display_name, code=returncode)
            if tail:
                error += f"\n{tail}"
            # Some CLIs report the failure as ERROR events on stdout (e.g.
            # opencode) and leave stderr empty: without this the error is a
            # bare "exited with code N" with no diagnosable detail.
            cli_errors = [part.strip() for part in error_parts if part.strip()]
            if cli_errors:
                error += "\n" + "\n".join(cli_errors[-3:])
        # The usage classifier sees the raw tail: the trimmed message may drop
        # lines that carry the rate-limit hint.
        raw_tail = "\n".join(stderr_parts[-_STDERR_TAIL_LINES:])
        usage_wait = (
            self._classify_usage_wait(f"{error}\n{raw_tail}", error_parts) if not ok else None
        )
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
