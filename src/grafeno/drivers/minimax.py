"""Driver for MiniMax Code CLI (npm ``@minimax-ai/code``, executable ``mcode``).

Non-interactive mode (verified against mcode 0.7.4; the prompt travels via
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
  providers; the MiniMax-managed ones come with ``models: []``. Their
  catalogue is read from mcode's ``config.yaml`` (``$MINIMAX_DATA_DIR`` or
  ``$MAVIS_DATA_DIR`` or ``~/.minimax``), honouring each provider's
  ``whitelist``; a static list is the fallback when it cannot be read.
- Effort: two mechanisms per model, never combined. The levels of
  ``thinking.effortOptions`` (``default`` is not offered: empty = CLI default)
  go through ``--effort``; the keys of ``variants`` go as a ``#<variant>``
  model suffix, except in ``forced_on`` models (thinking cannot be toggled:
  ``#none-thinking`` fails with "Invalid model reasoning"), which offer none.
  Unknown levels fail the run (exit 2, or "Invalid model variant"), so
  ``effort_levels_async`` validates them and ``CLIDriver.run`` drops the
  unsupported ones with a notice. MiniMax-M2.7 no longer exists in the
  catalogue (mcode runs it as MiniMax-M3).
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
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage, format_error_message

_KEY_RE = re.compile(
    r"""^(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'#-][^:]*?))\s*:(?:\s+(?P<value>.*))?$"""
)


def _split_key(line: str) -> tuple[str, str] | None:
    """``key: rest`` -> (key, rest); None when the line is not a mapping entry."""
    match = _KEY_RE.match(line)
    if not match:
        return None
    key = next((g for g in (match["dq"], match["sq"], match["bare"]) if g is not None), "")
    return key, (match["value"] or "").strip()


def _scalar(value: str) -> Any:
    """Interpret a YAML scalar (quotes, booleans, integers, empty collections)."""
    value = value.strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        return value[1:-1]
    comment = value.find(" #")
    if comment >= 0:
        value = value[:comment].rstrip()
    if value in ("true", "false"):
        return value == "true"
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value == "[]":
        return []
    if value == "{}":
        return {}
    return value


def parse_block_yaml(text: str) -> dict[str, Any]:
    """Minimal reader for the block YAML written by mcode (js-yaml dump).

    Supports nested mappings, block sequences of scalars, quoted keys and
    scalars, booleans, integers and empty flow collections (``[]``/``{}``).
    Unsupported constructs (sequences of mappings, multi-line scalars,
    anchors) are skipped, never raised: the catalogue is best effort.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, Any]] = [(-1, root)]  # (indent of the opening key, container)
    pending: tuple[int, dict[str, Any], str] | None = None  # key waiting for its block
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        is_item = stripped == "-" or stripped.startswith("- ")
        if pending:
            key_indent, owner, key = pending
            pending = None
            if indent > key_indent or (is_item and indent == key_indent):
                child: Any = [] if is_item else {}
                owner[key] = child
                stack.append((key_indent, child))
        while len(stack) > 1 and indent <= stack[-1][0] and not (
            is_item and isinstance(stack[-1][1], list) and indent == stack[-1][0]
        ):
            stack.pop()
        container = stack[-1][1]
        if is_item:
            item = stripped[1:].strip()
            if isinstance(container, list) and _split_key(item) is None:
                container.append(_scalar(item))
            continue  # sequences of mappings: skipped
        if not isinstance(container, dict):
            continue
        parsed = _split_key(stripped)
        if parsed is None:
            continue
        key, value = parsed
        if value == "":
            container[key] = None
            pending = (indent, container, key)
        else:
            container[key] = _scalar(value)
    return root


def load_yaml(text: str) -> Any:
    """``yaml.safe_load`` when PyYAML is installed, else ``parse_block_yaml``."""
    try:
        import yaml  # optional: only a dev dependency of GRAFENO
    except ImportError:
        return parse_block_yaml(text)
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return parse_block_yaml(text)


@dataclass(frozen=True)
class ModelEfforts:
    """Effort knobs of an mcode model."""

    efforts: tuple[str, ...] = ()    # ``--effort`` levels (thinking.effortOptions)
    variants: tuple[str, ...] = ()   # ``#variant`` model suffixes

    @property
    def levels(self) -> list[str]:
        """Levels offered in the UI: effort levels first, then variants."""
        return [*self.efforts, *(v for v in self.variants if v not in self.efforts)]


class MiniMaxDriver(CLIDriver):
    name = "minimax"
    display_name = "MiniMax Code CLI"
    executable = "mcode"
    init_command = "/init"

    # Fallback catalogue when mcode's config.yaml cannot be read.
    STATIC_MODELS = ("minimax/MiniMax-M3", "minimax/MiniMax-M3.1-Flash")
    STATIC_CATALOG: dict[str, ModelEfforts] = {
        "minimax/MiniMax-M3": ModelEfforts(variants=("none-thinking", "thinking")),
        "minimax/MiniMax-M3.1-Flash": ModelEfforts(efforts=("low", "medium", "high", "xhigh", "max")),
    }
    # (path, st_mtime_ns, catalogue) of the last config.yaml read.
    _catalog_cache: tuple[str, int, dict[str, ModelEfforts]] | None = None

    def config_path(self) -> Path:
        """mcode config: ``$MINIMAX_DATA_DIR`` (or ``$MAVIS_DATA_DIR``) or ``~/.minimax``."""
        data_dir = (os.environ.get("MINIMAX_DATA_DIR") or os.environ.get("MAVIS_DATA_DIR") or "").strip()
        base = Path(data_dir).expanduser() if data_dir else Path.home() / ".minimax"
        return base / "config.yaml"

    @staticmethod
    def parse_catalog(data: Any) -> dict[str, ModelEfforts]:
        """``{provider/model: ModelEfforts}`` from the parsed config.yaml."""
        providers = data.get("provider") if isinstance(data, dict) else None
        if not isinstance(providers, dict):
            return {}

        def mapping(value: Any) -> dict:
            return value if isinstance(value, dict) else {}

        catalog: dict[str, ModelEfforts] = {}
        for provider_id, pdata in providers.items():
            models = mapping(pdata).get("models")
            if not isinstance(models, dict):
                continue
            whitelist = mapping(pdata).get("whitelist")
            allowed = {str(m) for m in whitelist} if isinstance(whitelist, list) and whitelist else None
            for model_id, mdata in models.items():
                if allowed is not None and str(model_id) not in allowed:
                    continue
                mdata = mapping(mdata)
                options = mapping(mdata.get("thinking")).get("effortOptions")
                efforts = tuple(
                    str(o) for o in (options if isinstance(options, list) else [])
                    if isinstance(o, str) and o and o != "default"
                )
                forced_on = mapping(mdata.get("thinking_config")).get("mode") == "forced_on"
                variants = () if forced_on else tuple(str(name) for name in mapping(mdata.get("variants")))
                catalog[f"{provider_id}/{model_id}"] = ModelEfforts(efforts, variants)
        return catalog

    def load_catalog(self) -> dict[str, ModelEfforts] | None:
        """Catalogue read from config.yaml (cached by mtime); None if unreadable or empty."""
        path = self.config_path()
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._catalog_cache
        if cached and cached[0] == str(path) and cached[1] == mtime:
            return cached[2] or None
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        catalog = self.parse_catalog(load_yaml(text))
        self._catalog_cache = (str(path), mtime, catalog)
        return catalog or None

    def catalog(self) -> dict[str, ModelEfforts]:
        """``load_catalog()`` or the static fallback."""
        return self.load_catalog() or dict(self.STATIC_CATALOG)

    def model_efforts(self, model: str) -> ModelEfforts | None:
        """Entry of ``model`` tolerating the provider prefix and a ``#variant`` suffix."""
        catalog = self.catalog()
        if model in catalog:
            return catalog[model]
        bare = model.split("#", 1)[0]
        if bare in catalog:
            return catalog[bare]
        tail = bare.rsplit("/", 1)[-1]
        matches = [key for key in catalog if key.rsplit("/", 1)[-1] == tail]
        return catalog[matches[0]] if len(matches) == 1 else None

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
            effort_flag = ""
            entry = self.model_efforts(model) if request.effort else None
            if entry and request.effort in entry.efforts:
                effort_flag = request.effort                    # --effort (effortOptions)
            elif entry and request.effort in entry.variants and "#" not in model:
                model = f"{model}#{request.effort}"             # thinking variant suffix
            command += ["--model", model]
            if effort_flag:
                command += ["--effort", effort_flag]
        if request.session_id:
            command += ["--session", request.session_id]
        return command

    def models_command(self) -> list[str]:
        return ["mcode", "provider", "list", "--json"]

    def update_command(self) -> list[str]:
        return ["mcode", "update"]

    def parse_models(self, output: str) -> list[str]:
        """Catalogue of config.yaml (static fallback) plus enabled custom providers."""
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []
        models = set(self.catalog())
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
        """Per-model levels from mcode's config.yaml (static fallback): no CLI command lists them."""
        return {model: entry.levels for model, entry in self.catalog().items() if entry.levels}

    async def effort_levels_async(self, model: str) -> list[str] | None:
        entry = self.model_efforts(model) if model else None
        return entry.levels if entry else []

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
