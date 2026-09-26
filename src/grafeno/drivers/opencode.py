"""Driver for OpenCode CLI (https://opencode.ai).

Non-interactive mode (the prompt travels via stdin, not argv), OpenCode 1.x:
    opencode run -m <provider/model> --format json --auto \
        --dir <workdir> [--variant <level>] [--session <id>] [--title <title>]

OpenCode 2.x (verified against v2.0.17) dropped ``--dir`` and ``--variant``:
the run uses the process cwd (the base already spawns in the workdir) and the
effort travels as a model suffix (``-m provider/model#<variant>``). The
project directory is taken from ``$PWD`` rather than the real cwd (verified:
with a stale ``PWD`` the agent worked in the parent's directory), so
``run_env`` points ``PWD`` at the workdir. The major version is detected once
with ``opencode --version``.

With ``--format json`` it emits JSONL events with fields like ``sessionID``,
``type`` ("text", "tool_use", "step_start", "error", ...) and ``part``.
The parsing is defensive: unknown events are shown as INFO. With no message
argument and a non-TTY stdin, ``opencode run`` reads the prompt from stdin.

Effort variants per model are listed with ``opencode models --verbose`` in
1.x (header ``provider/model`` followed by a multi-line JSON block) and with
``opencode api model.list`` in 2.x (a single JSON document
``{"data": [{"providerID", "id", "variants": [{"id": ...}]}]}``).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage, format_error_message


class OpenCodeDriver(CLIDriver):
    name = "opencode"
    display_name = "OpenCode CLI"
    executable = "opencode"
    init_command = "/init"
    # Cached major version of the installed CLI (None = not detected yet).
    _major: int | None = None

    def major_version(self) -> int:
        """Major version of the installed CLI, detected once (1 if unknown)."""
        if self._major is None:
            output = self._run_sync(["opencode", "--version"]) or ""
            match = re.search(r"(\d+)\.\d+", output)
            self._major = int(match.group(1)) if match else 1
        return self._major

    def stdin_prompt(self) -> bool:
        return True  # opencode run reads the prompt from stdin when omitted

    def run_env(self, request: RunRequest) -> dict[str, str] | None:
        if self.major_version() < 2:
            return None
        return {**os.environ, "PWD": str(request.workdir)}  # 2.x resolves the project from $PWD

    def build_command(self, request: RunRequest) -> list[str]:
        # No prompt in argv: Windows .cmd shims cut quoted args at newlines.
        command = ["opencode", "run", "--format", "json", "--auto"]
        if self.major_version() >= 2:
            # 2.x: no --dir (cwd is the workdir) and no --variant (model suffix).
            model = request.model
            if model and request.effort and "#" not in model:
                model = f"{model}#{request.effort}"
            if model:
                command += ["-m", model]
        else:
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

    def update_command(self) -> list[str]:
        return ["opencode", "upgrade"]

    def parse_models(self, output: str) -> list[str]:
        return sorted(
            line.strip()
            for line in output.splitlines()
            if line.strip() and "/" in line and " " not in line.strip()
        )

    def variants_command(self) -> list[str]:
        if self.major_version() >= 2:
            return ["opencode", "api", "model.list"]
        return ["opencode", "models", "--verbose"]

    def parse_variants(self, output: str) -> dict[str, list[str]]:
        """Extract ``variants`` per model from the variants command output.

        Accepts both the 2.x ``opencode api model.list`` JSON document and the
        1.x ``opencode models --verbose`` format.
        """
        stripped = output.strip()
        if stripped.startswith("{") and '"data"' in stripped:
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("data"), list):
                return self._parse_model_list(data["data"])
        return self._parse_verbose_models(output)

    @staticmethod
    def _parse_model_list(models: list[Any]) -> dict[str, list[str]]:
        """Variants per model from the 2.x ``model.list`` entries."""
        result: dict[str, list[str]] = {}
        for entry in models:
            if not isinstance(entry, dict):
                continue
            provider = entry.get("providerID")
            model_id = entry.get("id") or entry.get("modelID")
            variants = entry.get("variants") or []
            if isinstance(variants, dict):
                keys = [str(key) for key in variants]
            else:
                keys = [
                    str(item.get("id")) for item in variants
                    if isinstance(item, dict) and item.get("id")
                ]
            if provider and model_id and keys:
                result[f"{provider}/{model_id}"] = sorted(keys)
        return result

    def _parse_verbose_models(self, output: str) -> dict[str, list[str]]:
        """Extract ``variants`` per model from the output of ``opencode models --verbose``.

        The output alternates a ``provider/model`` line with a multi-line JSON
        block that may include ``"variants": {level: {...}, ...}``. It is NOT
        JSONL: each block has to be parsed whole. Models whose ``variants``
        are empty are omitted.
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
            title = state.get("title") or ""
            if not title or title == tool:  # 2.x repeats the tool name as title
                title = (state.get("input") or {}).get("command") or ""
            summary = f"{tool}: {title}" if title else str(tool)
            return RunEvent(EventKind.TOOL, summary[:200]), session_id

        if event_type == "error":
            message = format_error_message(payload, payload.get("error"), part.get("message"))
            return RunEvent(EventKind.ERROR, message[:500]), session_id

        if event_type in {"step_start", "step_finish", "session_start", "session_end"}:
            return None, session_id  # internal noise: recorded in the raw log

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
