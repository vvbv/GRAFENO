"""Driver for MiniMax Code CLI (npm ``@minimax-ai/code``, executable ``mcode``).

Non-interactive mode (verified against mcode 0.5.3; the prompt travels via
stdin, not argv):
    mcode exec --input - --output-format stream-json --permission full \
        --cwd <workdir> [--model <provider/model>[#<variant>]] [--session <id>]

- ``--input -`` reads the prompt from stdin (the only supported source).
- ``--permission full`` auto-approves tools (``smart`` is the default; ``ask``
  requires the TUI).
- Model as ``<providerId>/<modelId>`` (e.g. ``minimax/MiniMax-M3``); custom
  providers use their own ``providerId``.
- Session continuation with ``--session <id>`` (emits ``session.resumed``).
- Models: ``mcode provider list --json`` only lists the models of custom
  providers; the MiniMax-managed ones (OAuth / API key) come with
  ``models: []``, so the built-in catalogue is added statically.
- Effort: ``--effort`` is rejected by the built-in models ("does not support
  reasoning effort selection", exit code 2), so it is NOT used. The only
  working knob is the thinking variant as a model suffix
  (``minimax/MiniMax-M3#none-thinking``); an unknown suffix fails the run
  ("Invalid model variant"), so ``RunRequest.effort`` is applied only when it
  is a known variant of the selected model and ignored otherwise.
- Native self-update: ``mcode update``. ``/init`` (the TUI slash command
  behind ``mcode init``) generates AGENTS.md.

Verified real stream-json events (every line carries ``sessionId``):
  ``{"type":"exec.started"}``, ``{"type":"session.started"}`` (or
  ``session.resumed``), ``{"type":"turn.started"}``: raw log only
  ``{"type":"item.started"|"item.updated","item":{...,"contentDelta":"..."}}``
  (streaming deltas: raw log only; ``item.completed`` carries the full item)
  ``{"type":"item.completed","item":{"type":"agent_message","content":"..."}}``
  ``{"type":"item.completed","item":{"type":"reasoning","content":"..."}}`` (noise)
  ``{"type":"item.completed","item":{"type":"tool_call","toolCall":{"name":"bash",
     "input":{"command":"ls"},"output":{...}}}}``
  ``{"type":"turn.completed","usage":{"inputTokens":N,"outputTokens":M,
     "cacheReadTokens":R,"cacheWriteTokens":W,"totalTokens":...}}``
  ``{"type":"turn.failed","error":{"category":"runtime","message":"..."}}``
  ``{"type":"exec.completed","result":{...}}`` (duplicates the turn outcome)

Verified notes: ``inputTokens`` EXCLUDES cached input (it is 0 on a fully
cached MiniMax-M2.7 turn), so cache reads and writes are added to it, like
the claude driver does. Only ``turn.completed`` is counted: ``exec.completed``
repeats the same usage nested under ``result``. Pre-flight errors (e.g. an
unsupported ``--effort``) are plain stderr text with no JSON events.
"""

from __future__ import annotations

import json
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage, format_error_message


class MiniMaxDriver(CLIDriver):
    name = "minimax"
    display_name = "MiniMax Code CLI"
    executable = "mcode"
    init_command = "/init"

    # MiniMax-managed models: ``provider list`` reports them with no models.
    STATIC_MODELS = (
        "minimax/MiniMax-M3", "minimax/MiniMax-M2.7", "minimax/MiniMax-M2.7-highspeed",
    )
    # Thinking variants per model, applied as a ``#<variant>`` model suffix.
    VARIANTS = {"minimax/MiniMax-M3": ("none-thinking", "thinking")}

    def stdin_prompt(self) -> bool:
        return True  # mcode exec --input - reads the prompt from stdin

    def build_command(self, request: RunRequest) -> list[str]:
        # No prompt in argv: Windows .cmd shims cut quoted args at newlines.
        command = [
            "mcode", "exec", "--input", "-",
            "--output-format", "stream-json",
            "--permission", "full",  # auto-approve tools
            "--cwd", str(request.workdir),
        ]
        if request.model:
            model = request.model
            if request.effort in self.VARIANTS.get(model, ()):
                model = f"{model}#{request.effort}"
            command += ["--model", model]
        if request.session_id:
            command += ["--session", request.session_id]
        return command

    def models_command(self) -> list[str]:
        return ["mcode", "provider", "list", "--json"]

    def update_command(self) -> list[str]:
        return ["mcode", "update"]

    def parse_models(self, output: str) -> list[str]:
        """Built-in catalogue plus the models of enabled custom providers."""
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []
        models = set(self.STATIC_MODELS)
        providers = data.get("providers") if isinstance(data, dict) else None
        for provider in providers if isinstance(providers, list) else []:
            if not isinstance(provider, dict) or provider.get("enabled") is False:
                continue
            provider_id = str(provider.get("providerId") or "")
            for model in provider.get("models") or []:
                model_id = model.get("modelId") if isinstance(model, dict) else None
                if provider_id and isinstance(model_id, str) and model_id:
                    models.add(f"{provider_id}/{model_id}")
        return sorted(models)

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """Static variants: mcode exposes no command to list them."""
        return {model: list(levels) for model, levels in self.VARIANTS.items()}

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = payload.get("sessionId") or payload.get("session_id")
        event_type = str(payload.get("type", ""))

        if event_type == "item.completed":
            item = payload.get("item")
            return self._decode_item(item if isinstance(item, dict) else {}), session_id

        if event_type in {"turn.failed", "error"}:
            message = format_error_message(payload, payload.get("error"), payload.get("message"))
            return RunEvent(EventKind.ERROR, str(message)[:500]), session_id

        if event_type in {
            "item.started", "item.updated",       # streaming deltas
            "exec.started", "exec.completed",     # the result repeats the turn outcome
            "turn.started", "turn.completed",     # usage is extracted by extract_usage
        } or event_type.startswith("session."):
            return None, session_id

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id

    # Tool input keys shown next to the tool name, in preference order.
    _TOOL_DETAIL_KEYS = ("command", "path", "file_path", "pattern", "query", "url", "description")

    @classmethod
    def _decode_item(cls, item: dict[str, Any]) -> RunEvent | None:
        """Interpret a completed item: message -> TEXT, tool call -> TOOL."""
        item_type = str(item.get("type", ""))
        if item_type == "agent_message":
            text = cls._extract_text(item.get("content"))
            return RunEvent(EventKind.TEXT, text) if text else None
        if item_type == "tool_call":
            tool_call = item.get("toolCall")
            return RunEvent(EventKind.TOOL, cls._tool_label(tool_call)[:200])
        return None  # reasoning and unknown items: raw log only

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

    @classmethod
    def _tool_label(cls, tool_call: Any) -> str:
        """``"bash: ls -la"`` instead of a bare ``"bash"``."""
        if not isinstance(tool_call, dict):
            return "tool"
        name = str(tool_call.get("name") or "tool")
        args = tool_call.get("input")
        if isinstance(args, dict):
            for key in cls._TOOL_DETAIL_KEYS:
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    detail = " ".join(value.split())
                    if len(detail) > 120:
                        detail = detail[:117] + "..."
                    return f"{name}: {detail}"
        return name

    # ------------------------------------------------------------ #
    # Input token fields of ``usage``; together they are the total input.
    _INPUT_USAGE_KEYS = ("inputTokens", "cacheReadTokens", "cacheWriteTokens")

    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        if str(payload.get("type", "")) != "turn.completed":
            return None
        usage_dict = payload.get("usage")
        if not isinstance(usage_dict, dict):
            return None
        try:
            # inputTokens excludes cached input: add cache reads and writes.
            usage = TokenUsage(
                input=sum(int(usage_dict.get(key) or 0) for key in self._INPUT_USAGE_KEYS),
                output=int(usage_dict.get("outputTokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
