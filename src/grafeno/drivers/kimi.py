"""Driver for Kimi Code CLI (https://moonshotai.github.io/kimi-code/).

Non-interactive mode:
    kimi -p "<prompt>" -m <alias> --output-format stream-json

Notes verified against kimi 2.4.1:
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
- It has no effort flag. The levels of each model are the ``supportEfforts``
  of ``kimi provider list --json`` (models without it offer none) and the
  chosen level is applied with the ``KIMI_MODEL_THINKING_EFFORT`` environment
  variable (``run_env``). A value outside ``supportEfforts`` makes the run
  fail with HTTP 400, so ``effort_levels_async`` validates it and
  ``CLIDriver.run`` drops it with a notice. With an empty model (the CLI
  default, unknown here) the effort is ignored.
- The prompt travels in argv (``-p``): kimi does not document reading it
  from stdin, so it cannot use the stdin fallback of the base driver.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage, format_error_message, model_levels


class KimiDriver(CLIDriver):
    name = "kimi"
    display_name = "Kimi Code CLI"
    executable = "kimi"
    # kimi does not expose an init command in non-interactive mode: generic prompt.
    init_command = ""
    # Environment variable kimi reads as the forced thinking effort of the run
    # (verified against kimi 2.4.1; ``[thinking].forcedEffort`` in its config).
    EFFORT_ENV = "KIMI_MODEL_THINKING_EFFORT"
    EFFORTS_TTL_SECONDS = 600  # run-time cache of the per-model effort catalogue
    _efforts_cache: tuple[float, dict[str, list[str]]] | None = None

    def build_command(self, request: RunRequest) -> list[str]:
        command = ["kimi", "-p", request.prompt, "--output-format", "stream-json"]
        if request.model:
            command += ["-m", request.model]
        if request.session_id:
            command += ["-S", request.session_id]
        return command

    def models_command(self) -> list[str]:
        return ["kimi", "provider", "list", "--json"]

    def variants_command(self) -> list[str]:
        return ["kimi", "provider", "list", "--json"]

    def parse_variants(self, output: str) -> dict[str, list[str]]:
        """``supportEfforts`` of each model alias (models without it are omitted)."""
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return {}
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, dict):
            return {}
        result: dict[str, list[str]] = {}
        for alias, entry in models.items():
            if not isinstance(entry, dict):
                continue
            efforts = entry.get("supportEfforts") or entry.get("support_efforts")
            if not isinstance(efforts, list):
                continue
            levels = [
                level.strip().lower() for level in efforts
                if isinstance(level, str) and level.strip()
            ]
            if levels:
                result[str(alias)] = levels
        return result

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """List the variants and refresh the run-time cache when the CLI answered."""
        variants = await super().list_variants_async(timeout)
        if variants:
            self._efforts_cache = (time.monotonic(), variants)
        return variants

    async def effort_levels_async(self, model: str) -> list[str] | None:
        if not model:
            return []  # the CLI default model is unknown here: never guess a level
        cached = self._efforts_cache
        if cached is None or time.monotonic() - cached[0] >= self.EFFORTS_TTL_SECONDS:
            await self.list_variants_async()
            cached = self._efforts_cache
        return model_levels(cached[1] if cached else {}, model)

    def run_env(self, request: RunRequest) -> dict[str, str] | None:
        if not request.effort:
            return None
        return {**os.environ, self.EFFORT_ENV: request.effort}

    def update_command(self) -> list[str]:
        return ["kimi", "update"]

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
            text = self._extract_text(content)
            if not text and content is not None and not isinstance(content, (dict, list)):
                text = str(content).strip()
            summary = text.splitlines()[0] if text else "tool"
            return RunEvent(EventKind.TOOL, summary[:200]), session_id

        if role in {"meta", "system"} or event_type in {"init", "system"}:
            return None, session_id  # versions, resume hints...: raw log only

        if event_type == "error" or payload.get("error"):
            message_text = format_error_message(payload, payload.get("error"), payload.get("message"))
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
