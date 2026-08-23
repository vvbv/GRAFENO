"""Driver para Kimi Code CLI (https://moonshotai.github.io/kimi-code/).

Modo no-interactivo:
    kimi -p "<prompt>" -m <alias> --output-format stream-json

Notas verificadas contra kimi 0.37:
- ``-p/--prompt`` NO admite ``--auto`` ni ``-y``; en modo prompt las
  herramientas se auto-aprueban (no hay interacción posible).
- No tiene flag de directorio: el workdir se pasa como ``cwd`` del subproceso.
- Eventos stream-json reales:
  ``{"role":"assistant","content":"..."}``,
  ``{"role":"assistant","tool_calls":[...]}``,
  ``{"role":"tool","content":"..."}``,
  ``{"role":"meta","type":"session.resume_hint","session_id":"..."}``.
- Continuación de sesión con ``-S <id>`` (mejor esfuerzo).
- Los modelos se obtienen de ``kimi provider list --json``.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest


class KimiDriver(CLIDriver):
    name = "kimi"
    display_name = "Kimi Code CLI"
    executable = "kimi"
    # kimi no expone comando init en modo no-interactivo: prompt genérico.
    init_command = ""

    def build_command(self, request: RunRequest) -> list[str]:
        command = ["kimi", "-p", request.prompt, "--output-format", "stream-json"]
        if request.model:
            command += ["-m", request.model]
        if request.session_id:
            command += ["-S", request.session_id]
        return command

    def models_command(self) -> list[str]:
        return ["kimi", "provider", "list", "--json"]

    def parse_models(self, output: str) -> list[str]:
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []
        return sorted(str(alias) for alias in (data.get("models") or {}))

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = (
            payload.get("session_id")
            or payload.get("sessionId")
            or payload.get("sessionID")
        )
        role = str(payload.get("role", ""))
        event_type = str(payload.get("type", ""))

        if role == "assistant":
            tool_calls = payload.get("tool_calls") or []
            if tool_calls:
                names = [
                    str((call.get("function") or {}).get("name", "tool"))
                    for call in tool_calls
                ]
                return RunEvent(EventKind.TOOL, ", ".join(names)[:200]), session_id
            text = self._extract_text(payload.get("content"))
            return (RunEvent(EventKind.TEXT, text) if text else None), session_id

        if role == "tool":
            content = payload.get("content")
            summary = str(content).strip().splitlines()[0] if content else "tool"
            return RunEvent(EventKind.TOOL, summary[:200]), session_id

        if role in {"meta", "system"} or event_type in {"init", "system"}:
            return None, session_id  # versiones, resume hints…: solo al log crudo

        if event_type == "error" or payload.get("error"):
            message_text = payload.get("error") or payload.get("message") or str(payload)
            return RunEvent(EventKind.ERROR, str(message_text)[:500]), session_id

        # Formato alternativo {"type":"assistant","message":{"content":[...]}}
        message = payload.get("message") or {}
        content = message.get("content", payload.get("content"))
        if event_type in {"assistant", "text", "message"} or content:
            text = self._extract_text(content)
            if text:
                return RunEvent(EventKind.TEXT, text), session_id

        return None, session_id

    @staticmethod
    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(part for part in parts if part).strip()
        return ""
