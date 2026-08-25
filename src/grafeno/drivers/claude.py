"""Driver for Claude Code CLI (https://docs.anthropic.com/en/docs/claude-code).

Non-interactive mode (verified against Claude Code 2.1.237):
    claude -p "<prompt>" --output-format stream-json --verbose \
        --dangerously-skip-permissions [--model <alias>] [--effort <level>] \
        [--resume <session_id>]

- ``stream-json`` requires ``--verbose`` in ``-p`` mode.
- ``--dangerously-skip-permissions`` auto-approves tools.
- ``--model`` accepts aliases (``opus``, ``sonnet``, ``haiku``, ``fable``) or
  a full name.
- ``--effort`` accepts levels (``low``, ``medium``, ``high``, ``xhigh``, ``max``).
- Session continuation with ``--resume <session_id>``.
- It has NO directory flag: the workdir is the subprocess ``cwd``.
- It does NOT expose a model listing command: static list (best effort).
- Claude Code's ``/init`` generates ``CLAUDE.md``, not ``AGENTS.md``:
  ``init_command = ""`` (generic prompt).

Verified real stream-json events:
  ``{"type":"system","subtype":"init","session_id":"...","cwd":"...",...}``
  ``{"type":"system","subtype":"hook_started",...}`` (noise: raw log only)
  ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
  ``{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",...}]}}``
  ``{"type":"result","subtype":"success","session_id":"...","usage":{...}}``

Verified notes: ``usage`` may include ``cache_creation_input_tokens`` and
``cache_read_input_tokens`` which are IGNORED (only direct input/output
counted). The session id is carried in ``session_id`` for both ``system``
and ``result`` events.
"""

from __future__ import annotations

from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage


class ClaudeDriver(CLIDriver):
    name = "claude"
    display_name = "Claude Code CLI"
    executable = "claude"
    # Claude Code's /init generates CLAUDE.md, not AGENTS.md: generic prompt.
    init_command = ""

    # claude does not expose model listing: static aliases (best effort).
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
        return self.list_models()  # no subprocess: static list

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """Static variants: claude exposes no command to list them."""
        return {model: list(self.EFFORT_LEVELS) for model in self.list_models()}

    def update_command(self) -> list[str]:
        return ["claude", "update"]

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
            return None, session_id  # OK result: usage is extracted by extract_usage

        if event_type == "system":
            return None, session_id  # init, hook_started, ...: raw log only

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id

    @staticmethod
    def _decode_content(content: Any) -> RunEvent | None:
        """Interpret the ``content`` block list of an ``assistant`` event."""
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
                # cache_creation/cache_read are ignored: only direct input/output.
                output=int(usage_dict.get("output_tokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
