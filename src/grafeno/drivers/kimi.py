"""Driver para Kimi Code CLI (https://moonshotai.github.io/kimi-code/).

Modo no-interactivo:
    kimi -p "<prompt>" -m <alias> --auto --output-format stream-json

Con ``stream-json`` emite eventos JSONL. No tiene flag de directorio, así
que el workdir se pasa como ``cwd`` del subproceso. La continuación de
sesión usa ``-S <id>`` (mejor esfuerzo). Los modelos se obtienen de
``kimi provider list --json``.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest


class KimiDriver(CLIDriver):
    name = "kimi"
    display_name = "Kimi Code CLI"
    executable = "kimi"

    def build_command(self, request: RunRequest) -> list[str]:
        command = [
            "kimi",
            "-p",
            request.prompt,
            "--auto",
            "--output-format",
            "stream-json",
        ]
        if request.model:
            command += ["-m", request.model]
        if request.session_id:
            command += ["-S", request.session_id]
        return command

    def list_models(self) -> list[str]:
        output = self._run_sync(["kimi", "provider", "list", "--json"])
        if not output:
            return []
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
        event_type = str(payload.get("type", ""))

        # Formato habitual: {"type":"assistant","message":{"content":[{"type":"text",...}]}}
        message = payload.get("message") or {}
        content = message.get("content", payload.get("content"))
        if event_type in {"assistant", "text", "message"} or content:
            text = self._extract_text(content)
            if text:
                return RunEvent(EventKind.TEXT, text), session_id

        if event_type in {"tool_use", "tool_call"}:
            tool = payload.get("name") or payload.get("tool") or "tool"
            return RunEvent(EventKind.TOOL, str(tool)[:200]), session_id

        if event_type == "error" or payload.get("error"):
            message_text = payload.get("error") or payload.get("message") or str(payload)
            return RunEvent(EventKind.ERROR, str(message_text)[:500]), session_id

        if event_type in {"init", "system", "result", "done"}:
            if isinstance(payload.get("result"), str) and payload["result"].strip():
                return RunEvent(EventKind.TEXT, payload["result"].strip()), session_id
            return None, session_id

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
