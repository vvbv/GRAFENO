"""Driver for Kimi Code CLI (https://moonshotai.github.io/kimi-code/).

Non-interactive mode:
    kimi -p "<prompt>" -m <alias> --output-format stream-json

Notes verified against kimi 0.37:
- ``-p/--prompt`` does NOT accept ``--auto`` or ``-y``; in prompt mode tools
  are auto-approved (no interaction is possible).
- It has no directory flag: the workdir is passed as the subprocess ``cwd``.
- Real stream-json events:
  ``{"role":"assistant","content":"..."}``,
  ``{"role":"assistant","tool_calls":[...]}``,
  ``{"role":"tool","content":"..."}``,
  ``{"role":"meta","type":"session.resume_hint","session_id":"..."}``.
- Session continuation with ``-S <id>`` (best effort).
- Models are obtained from ``kimi provider list --json``.
- It has no model effort flag: ``effort`` is ignored.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage


class KimiDriver(CLIDriver):
    name = "kimi"
    display_name = "Kimi Code CLI"
    executable = "kimi"
    # kimi does not expose an init command in non-interactive mode: generic prompt.
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
            return None, session_id  # versions, resume hints...: raw log only

        if event_type == "error" or payload.get("error"):
            message_text = payload.get("error") or payload.get("message") or str(payload)
            return RunEvent(EventKind.ERROR, str(message_text)[:500]), session_id

        # Alternate format {"type":"assistant","message":{"content":[...]}}
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

    # ------------------------------------------------------------ #
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage_dict = payload.get("usage")
        if not isinstance(usage_dict, dict):
            message = payload.get("message")
            if isinstance(message, dict) and isinstance(message.get("usage"), dict):
                usage_dict = message["usage"]
        if not isinstance(usage_dict, dict):
            return None
        try:
            usage = TokenUsage(
                input=int(usage_dict.get("input_tokens") or usage_dict.get("prompt_tokens") or 0),
                output=int(usage_dict.get("output_tokens") or usage_dict.get("completion_tokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
