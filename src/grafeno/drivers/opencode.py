"""Driver para OpenCode CLI (https://opencode.ai).

Modo no-interactivo:
    opencode run "<prompt>" -m <provider/model> --format json --auto \
        --dir <workdir> [--session <id>] [--title <título>]

Con ``--format json`` emite eventos JSONL con campos como ``sessionID``,
``type`` ("text", "tool_use", "step_start", "error", …) y ``part``.
El parseo es defensivo: eventos desconocidos se muestran como INFO.
"""

from __future__ import annotations

from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest


class OpenCodeDriver(CLIDriver):
    name = "opencode"
    display_name = "OpenCode CLI"
    executable = "opencode"
    init_command = "/init"

    def build_command(self, request: RunRequest) -> list[str]:
        command = ["opencode", "run", request.prompt, "--format", "json", "--auto"]
        if request.model:
            command += ["-m", request.model]
        command += ["--dir", str(request.workdir)]
        if request.session_id:
            command += ["--session", request.session_id]
        if request.title:
            command += ["--title", request.title]
        return command

    def models_command(self) -> list[str]:
        return ["opencode", "models"]

    def parse_models(self, output: str) -> list[str]:
        return sorted(
            line.strip()
            for line in output.splitlines()
            if line.strip() and "/" in line and " " not in line.strip()
        )

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = (
            payload.get("sessionID")
            or payload.get("session_id")
            or (payload.get("part") or {}).get("sessionID")
        )
        event_type = str(payload.get("type", ""))
        part = payload.get("part") or {}

        if event_type == "text":
            text = part.get("text") or payload.get("text") or ""
            return (RunEvent(EventKind.TEXT, str(text)) if text else None), session_id

        if event_type == "tool_use":
            tool = part.get("tool") or part.get("name") or "tool"
            state = part.get("state") or {}
            title = state.get("title") or state.get("input", {}).get("command") or ""
            summary = f"{tool}: {title}" if title else str(tool)
            return RunEvent(EventKind.TOOL, summary[:200]), session_id

        if event_type == "error":
            message = payload.get("error") or part.get("message") or str(payload)
            return RunEvent(EventKind.ERROR, str(message)[:500]), session_id

        if event_type in {"step_start", "step_finish", "session_start", "session_end"}:
            return None, session_id  # ruido interno: se registra en el log crudo

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id
