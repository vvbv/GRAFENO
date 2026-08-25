"""Driver para Claude Code CLI (https://docs.anthropic.com/en/docs/claude-code).

Modo no-interactivo (verificado contra Claude Code 2.1.237):
    claude -p "<prompt>" --output-format stream-json --verbose \
        --dangerously-skip-permissions [--model <alias>] [--effort <nivel>] \
        [--resume <session_id>]

- ``stream-json`` requiere ``--verbose`` en modo ``-p``.
- ``--dangerously-skip-permissions`` auto-aprueba las herramientas.
- ``--model`` admite alias (``opus``, ``sonnet``, ``haiku``, ``fable``) o nombre.
- ``--effort`` admite niveles (``low``, ``medium``, ``high``, ``xhigh``, ``max``).
- Continuación de sesión con ``--resume <session_id>``.
- NO tiene flag de directorio: el workdir es el ``cwd`` del subproceso.
- NO expone comando de listado de modelos: lista estática (mejor esfuerzo).
- ``/init`` de Claude Code genera ``CLAUDE.md``, no ``AGENTS.md``:
  ``init_command = ""`` (prompt genérico).

Eventos stream-json reales verificados:
  ``{"type":"system","subtype":"init","session_id":"...","cwd":"...",...}``
  ``{"type":"system","subtype":"hook_started",...}`` (ruido: solo al log)
  ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
  ``{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",...}]}}``
  ``{"type":"result","subtype":"success","session_id":"...","usage":{...}}``

Notas verificadas: ``usage`` puede incluir ``cache_creation_input_tokens`` y
``cache_read_input_tokens`` que se IGNORAN (solo input/output directos). El id
de sesión viaja en ``session_id`` tanto en eventos ``system`` como ``result``.
"""

from __future__ import annotations

from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage


class ClaudeDriver(CLIDriver):
    name = "claude"
    display_name = "Claude Code CLI"
    executable = "claude"
    # El /init de Claude Code genera CLAUDE.md, no AGENTS.md: prompt genérico.
    init_command = ""

    # claude no expone listado de modelos: alias estáticos (mejor esfuerzo).
    STATIC_MODELS = ("opus", "sonnet", "haiku", "fable")
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

    def build_command(self, request: RunRequest) -> list[str]:
        command = [
            "claude", "-p", request.prompt,
            "--output-format", "stream-json", "--verbose",
            "--dangerously-skip-permissions",
        ]
        if request.model:
            command += ["--model", request.model]
        if request.effort:
            command += ["--effort", request.effort]
        if request.session_id:
            command += ["--resume", request.session_id]
        return command

    def list_models(self) -> list[str]:
        return sorted(self.STATIC_MODELS)

    async def list_models_async(self, timeout: float = 30.0) -> list[str]:
        return self.list_models()  # sin subproceso: lista estática

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """Variantes estáticas: claude no expone comando para listarlas."""
        return {model: list(self.EFFORT_LEVELS) for model in self.list_models()}

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = payload.get("session_id") or payload.get("sessionId")
        event_type = str(payload.get("type", ""))

        if event_type == "assistant":
            message = payload.get("message") or {}
            content = message.get("content")
            event = self._decode_content(content)
            return event, session_id

        if event_type == "result":
            if payload.get("is_error") or str(payload.get("subtype", "")).startswith("error"):
                message = payload.get("result") or payload.get("error") or str(payload)
                return RunEvent(EventKind.ERROR, str(message)[:500]), session_id
            return None, session_id  # resultado OK: el uso lo extrae extract_usage

        if event_type == "system":
            return None, session_id  # init, hook_started, ...: solo al log crudo

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id

    @staticmethod
    def _decode_content(content: Any) -> RunEvent | None:
        """Interpreta la lista de bloques ``content`` de un evento ``assistant``."""
        if isinstance(content, list):
            tools = [
                str(item.get("name", "tool"))
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_use"
            ]
            if tools:
                return RunEvent(EventKind.TOOL, ", ".join(tools)[:200])
            texts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            text = "\n".join(part for part in texts if part).strip()
            return RunEvent(EventKind.TEXT, text) if text else None
        if isinstance(content, str) and content.strip():
            return RunEvent(EventKind.TEXT, content.strip())
        return None

    # ------------------------------------------------------------ #
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage_dict = payload.get("usage")
        if not isinstance(usage_dict, dict):
            return None
        try:
            usage = TokenUsage(
                input=int(usage_dict.get("input_tokens") or 0),
                # Se ignoran cache_creation/cache_read: solo input/output directos.
                output=int(usage_dict.get("output_tokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
