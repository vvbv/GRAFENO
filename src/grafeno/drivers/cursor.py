"""Driver for Cursor Agent CLI (https://cursor.com, executable ``cursor-agent``).

Non-interactive mode (verified against cursor-agent 2026.09.08; the prompt
travels via stdin, not argv):
    cursor-agent -p --output-format stream-json --trust -f \
        [--model <id>] [--resume <chatId>]

- With no positional prompt argument, ``-p`` reads the prompt from stdin.
- ``--trust`` skips the interactive workspace-trust prompt (mandatory in
  fresh directories; without it the run stalls and exits).
- ``-f/--force`` auto-approves commands (like claude's
  ``--dangerously-skip-permissions``).
- Session continuation with ``--resume <chatId>`` (keeps the session id).
- It has NO directory flag: the workdir is the subprocess ``cwd``.
- Models are listed with ``cursor-agent models`` (``<id> - <description>``
  lines after an ``Available models`` header).
- Effort is NOT mapped: bracket overrides (``model[effort=high]``) are
  rejected for most models and the levels already live inside the model ids
  (``-low``/``-medium``/``-high``/``-xhigh``/``-max`` suffixes), so the base
  defaults apply (``variants_command() -> []``, ``parse_variants -> {}``)
  and ``build_command`` ignores ``RunRequest.effort``.
- Native self-update: ``cursor-agent update``.
- cursor-agent exposes no AGENTS.md init command: generic prompt.

Verified real stream-json events (Claude-Code-like dialect, camelCase usage):
  ``{"type":"system","subtype":"init","session_id":"...","model":"Auto"}``
  ``{"type":"user","message":{"role":"user","content":[...]}}`` (prompt echo)
  ``{"type":"thinking","subtype":"delta","text":"..."}`` (noise)
  ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
  ``{"type":"tool_call","subtype":"started","tool_call":{"shellToolCall":{"args":{"command":"..."},...}}}``
  ``{"type":"result","subtype":"success","is_error":false,"result":"...",
     "usage":{"inputTokens":N,"outputTokens":M,"cacheReadTokens":...}}``

Verified notes: ``cacheReadTokens``/``cacheWriteTokens`` are IGNORED (only
direct input/output counted). ``tool_call`` carries one key per tool kind
(``shellToolCall``, ``readToolCall``, ...); only ``started`` emits a TOOL
event (``completed`` duplicates it). Pre-flight errors (e.g. unknown model)
are plain non-JSON text with exit code 1.
"""

from __future__ import annotations

from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage, format_error_message


class CursorDriver(CLIDriver):
    name = "cursor"
    display_name = "Cursor Agent CLI"
    executable = "cursor-agent"
    # cursor-agent exposes no init command for AGENTS.md: generic prompt.
    init_command = ""

    def stdin_prompt(self) -> bool:
        return True  # cursor-agent -p reads the prompt from stdin when omitted

    def build_command(self, request: RunRequest) -> list[str]:
        # No prompt in argv: Windows .cmd shims cut quoted args at newlines.
        # Effort is ignored: the levels live inside the model ids themselves.
        command = [
            "cursor-agent", "-p",
            "--output-format", "stream-json",
            "--trust",  # skip the interactive workspace-trust prompt
            "-f",       # auto-approve commands
        ]
        if request.model:
            command += ["--model", request.model]
        if request.session_id:
            command += ["--resume", request.session_id]
        return command

    def models_command(self) -> list[str]:
        return ["cursor-agent", "models"]

    def update_command(self) -> list[str]:
        return ["cursor-agent", "update"]

    def parse_models(self, output: str) -> list[str]:
        """Parse ``cursor-agent models`` output (``<id> - <description>`` lines)."""
        return sorted(
            line.split(" - ", 1)[0].strip()
            for line in output.splitlines()
            if " - " in line and line.split(" - ", 1)[0].strip()
        )

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = payload.get("session_id") or payload.get("sessionId")
        event_type = str(payload.get("type", ""))
        subtype = str(payload.get("subtype", ""))

        if event_type == "assistant":
            message = payload.get("message") or {}
            event = self._decode_content(message.get("content"))
            return event, session_id

        if event_type == "tool_call":
            if subtype != "started":
                return None, session_id  # completed duplicates the summary
            return self._decode_tool_call(payload.get("tool_call")), session_id

        if event_type == "result":
            if payload.get("is_error") or subtype.startswith("error"):
                message = format_error_message(payload, payload.get("result"), payload.get("error"))
                return RunEvent(EventKind.ERROR, str(message)[:500]), session_id
            return None, session_id  # OK result: usage is extracted by extract_usage

        if event_type in {"system", "user", "thinking"}:
            return None, session_id  # init, prompt echo, thinking deltas: raw log only

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id

    @staticmethod
    def _decode_content(content: Any) -> RunEvent | None:
        """Interpret the ``content`` block list of an ``assistant`` event."""
        if isinstance(content, list):
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

    @staticmethod
    def _decode_tool_call(tool_call: Any) -> RunEvent:
        """Summarize a ``tool_call`` object keyed by tool kind.

        Shape: ``{"shellToolCall": {"args": {"command": ...}, "description": ...}}``.
        The tool name is the key with its ``ToolCall`` suffix stripped.
        """
        if not isinstance(tool_call, dict) or not tool_call:
            return RunEvent(EventKind.TOOL, "tool")
        key = str(next(iter(tool_call)))
        name = key[: -len("ToolCall")] if key.endswith("ToolCall") else key
        detail = tool_call.get(key)
        detail = detail if isinstance(detail, dict) else {}
        args = detail.get("args")
        args = args if isinstance(args, dict) else {}
        summary = args.get("command") or detail.get("description") or name
        text = f"{name}: {summary}" if summary != name else name
        return RunEvent(EventKind.TOOL, str(text)[:200])

    # ------------------------------------------------------------ #
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        usage_dict = payload.get("usage")
        if not isinstance(usage_dict, dict):
            return None
        try:
            usage = TokenUsage(
                input=int(usage_dict.get("inputTokens") or usage_dict.get("input_tokens") or 0),
                # cacheRead/cacheWrite are ignored: only direct input/output.
                output=int(usage_dict.get("outputTokens") or usage_dict.get("output_tokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
