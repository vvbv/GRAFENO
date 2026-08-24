"""Driver para OpenCode CLI (https://opencode.ai).

Modo no-interactivo:
    opencode run "<prompt>" -m <provider/model> --format json --auto \
        --dir <workdir> [--variant <nivel>] [--session <id>] [--title <título>]

Con ``--format json`` emite eventos JSONL con campos como ``sessionID``,
``type`` ("text", "tool_use", "step_start", "error", …) y ``part``.
El parseo es defensivo: eventos desconocidos se muestran como INFO.

Las variantes de esfuerzo por modelo se listan con ``opencode models --verbose``
(cabecera ``proveedor/modelo`` seguida de un bloque JSON multilínea).
"""

from __future__ import annotations

import json
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage


class OpenCodeDriver(CLIDriver):
    name = "opencode"
    display_name = "OpenCode CLI"
    executable = "opencode"
    init_command = "/init"

    def build_command(self, request: RunRequest) -> list[str]:
        command = ["opencode", "run", request.prompt, "--format", "json", "--auto"]
        if request.model:
            command += ["-m", request.model]
        if request.effort:
            command += ["--variant", request.effort]
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

    def variants_command(self) -> list[str]:
        return ["opencode", "models", "--verbose"]

    def parse_variants(self, output: str) -> dict[str, list[str]]:
        """Extrae ``variants`` por modelo de la salida de ``opencode models --verbose``.

        La salida alterna una línea ``proveedor/modelo`` con un bloque JSON
        multilínea que puede incluir ``"variants": {nivel: {...}, ...}``. NO
        es JSONL: cada bloque hay que parsearlo entero. Se omiten los modelos
        cuyo ``variants`` está vacío.
        """
        result: dict[str, list[str]] = {}
        current_model = ""
        buffer: list[str] = []
        in_block = False
        for raw_line in output.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not in_block:
                if (
                    stripped
                    and "/" in stripped
                    and " " not in stripped
                    and not stripped.startswith("{")
                ):
                    current_model = stripped
                    buffer = []
                elif stripped == "{" and current_model:
                    buffer = ["{"]
                    in_block = True
                continue
            buffer.append(line if line else " ")
            joined = "\n".join(buffer)
            try:
                data = json.loads(joined)
            except json.JSONDecodeError:
                continue
            in_block = False
            variants = (data.get("variants") or {}) if isinstance(data, dict) else {}
            keys = sorted(str(key) for key in variants.keys()) if isinstance(variants, dict) else []
            if keys and current_model:
                result[current_model] = keys
            current_model = ""
            buffer = []
        return result

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

    # ------------------------------------------------------------ #
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        if str(payload.get("type", "")) != "step_finish":
            return None
        part = payload.get("part") or {}
        tokens = part.get("tokens") or payload.get("tokens") or {}
        try:
            usage = TokenUsage(
                input=int(tokens.get("input", 0) or 0),
                output=int(tokens.get("output", 0) or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
