"""Global GRAFENO configuration (~/.grafeno/config.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _toml, paths

KNOWN_CLIS = ("opencode", "kimi", "codex", "claude")
PROJECT_CONFIG_FILE = ".grafeno.toml"

# Telegram integration defaults (OpenAI-compatible endpoints; Groq by default).
TELEGRAM_TOKEN_ENV = "GRAFENO_TELEGRAM_TOKEN"
TELEGRAM_STT_KEY_ENV = "GRAFENO_TELEGRAM_STT_KEY"
TELEGRAM_TTS_KEY_ENV = "GRAFENO_TELEGRAM_TTS_KEY"
DEFAULT_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "whisper-large-v3-turbo"
DEFAULT_TTS_URL = "https://api.groq.com/openai/v1/audio/speech"
DEFAULT_TTS_MODEL = "canopylabs/orpheus-v1-english"
DEFAULT_TTS_VOICE = "troy"  # male voice


@dataclass
class RoleConfig:
    """CLI + model assigned to a pipeline role."""

    cli: str = "opencode"
    model: str = ""  # empty = CLI default model
    effort: str = ""  # empty = default effort level of the CLI/model

    def to_dict(self) -> dict[str, Any]:
        return {"cli": self.cli, "model": self.model, "effort": self.effort}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_cli: str) -> "RoleConfig":
        return cls(
            cli=str(data.get("cli", default_cli)),
            model=str(data.get("model", "")),
            effort=str(data.get("effort", "")),
        )


@dataclass
class AutomodeConfig:
    enabled: bool = False
    max_iterations: int = 5
    test_command: str = ""  # empty = no tests
    create_branch: bool = True
    confirm_plan: bool = False  # pause after the plan waiting for manual confirmation

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_iterations": self.max_iterations,
            "test_command": self.test_command,
            "create_branch": self.create_branch,
            "confirm_plan": self.confirm_plan,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutomodeConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            max_iterations=int(data.get("max_iterations", 5)),
            test_command=str(data.get("test_command", "")),
            create_branch=bool(data.get("create_branch", True)),
            confirm_plan=bool(data.get("confirm_plan", False)),
        )


@dataclass
class HookConfig:
    """Completion hook: shell command fired when stages finish."""

    command: str = ""  # empty = hook disabled
    stages: str = ""   # comma-separated stages; empty = none

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "stages": self.stages}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HookConfig":
        return cls(
            command=str(data.get("command", "")),
            stages=str(data.get("stages", "")),
        )


@dataclass
class EditorConfig:
    """Optional editor that opens when launching GRAFENO when the user
    enables it from the settings screen. Disabled by default: with no editor
    configured only the TUI is opened."""

    enabled: bool = False
    editor: str = ""        # empty = none (by default only grafeno opens)
    mode: str = "window"    # window | split | none (console editors only)
    side: str = "left"      # left = editor on the left, grafeno on the right

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "editor": self.editor,
            "mode": self.mode,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditorConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            editor=str(data.get("editor", "")),
            mode=str(data.get("mode", "window")),
            side=str(data.get("side", "left")),
        )


def _sanitize_key(value: str) -> str:
    """Clean a pasted API key: strips whitespace and label prefixes.

    Users paste values like ``"groq gsk_..."`` or ``"Bearer <key>"``; API
    keys never contain whitespace, so the last token is the key itself.
    """
    parts = value.split()
    return parts[-1] if parts else ""


@dataclass
class TelegramConfig:
    """Telegram bot integration: voice/text notes become GRAFENO tasks.

    The bot runs as a background worker inside the TUI. Speech-to-text and
    text-to-speech use OpenAI-compatible endpoints (Groq by default); the
    intent parser reuses an already-configured agent CLI. Secrets can also
    come from the environment (env var wins over the file), so the token
    never has to be written to disk.
    """

    enabled: bool = False
    bot_token: str = ""        # GRAFENO_TELEGRAM_TOKEN overrides
    allowed_chat_ids: str = "" # comma-separated chat ids; empty = deny all
    parser_cli: str = ""       # empty = planner role CLI
    parser_model: str = ""     # empty = default model of the parser CLI
    confirm_create: bool = True  # inline Create/Cancel buttons before creating
    group_all: bool = False    # True = answer every whitelisted group message (no mention needed)
    default_workdir: str = ""  # empty = app cwd
    stt_url: str = DEFAULT_STT_URL
    stt_key: str = ""          # GRAFENO_TELEGRAM_STT_KEY overrides
    stt_model: str = DEFAULT_STT_MODEL
    tts_enabled: bool = False  # voice replies are opt-in (they cost money)
    tts_url: str = DEFAULT_TTS_URL
    tts_key: str = ""          # GRAFENO_TELEGRAM_TTS_KEY overrides; empty = stt_key
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = DEFAULT_TTS_VOICE

    def resolve_token(self) -> str:
        """Bot token: the environment variable wins over the stored one."""
        return _sanitize_key(os.environ.get(TELEGRAM_TOKEN_ENV, "")) or _sanitize_key(self.bot_token)

    def resolve_stt_key(self) -> str:
        return _sanitize_key(os.environ.get(TELEGRAM_STT_KEY_ENV, "")) or _sanitize_key(self.stt_key)

    def resolve_tts_key(self) -> str:
        """TTS key: own env/field first, then the STT key (usually same provider)."""
        from_env = _sanitize_key(os.environ.get(TELEGRAM_TTS_KEY_ENV, ""))
        return from_env or _sanitize_key(self.tts_key) or self.resolve_stt_key()

    def chat_ids(self) -> set[int]:
        """Whitelisted chat ids (unparseable entries are ignored)."""
        ids: set[int] = set()
        for part in self.allowed_chat_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        return ids

    def masked_token(self) -> str:
        """Token for display: only the last 4 chars are shown."""
        token = self.resolve_token()
        return f"…{token[-4:]}" if len(token) > 4 else ("…" if token else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "bot_token": self.bot_token,
            "allowed_chat_ids": self.allowed_chat_ids,
            "parser_cli": self.parser_cli,
            "parser_model": self.parser_model,
            "confirm_create": self.confirm_create,
            "group_all": self.group_all,
            "default_workdir": self.default_workdir,
            "stt_url": self.stt_url,
            "stt_key": self.stt_key,
            "stt_model": self.stt_model,
            "tts_enabled": self.tts_enabled,
            "tts_url": self.tts_url,
            "tts_key": self.tts_key,
            "tts_model": self.tts_model,
            "tts_voice": self.tts_voice,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TelegramConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            bot_token=str(data.get("bot_token", "")),
            allowed_chat_ids=str(data.get("allowed_chat_ids", "")),
            parser_cli=str(data.get("parser_cli", "")),
            parser_model=str(data.get("parser_model", "")),
            confirm_create=bool(data.get("confirm_create", True)),
            group_all=bool(data.get("group_all", False)),
            default_workdir=str(data.get("default_workdir", "")),
            stt_url=str(data.get("stt_url", DEFAULT_STT_URL)),
            stt_key=str(data.get("stt_key", "")),
            stt_model=str(data.get("stt_model", DEFAULT_STT_MODEL)),
            tts_enabled=bool(data.get("tts_enabled", False)),
            tts_url=str(data.get("tts_url", DEFAULT_TTS_URL)),
            tts_key=str(data.get("tts_key", "")),
            tts_model=str(data.get("tts_model", DEFAULT_TTS_MODEL)),
            tts_voice=str(data.get("tts_voice", DEFAULT_TTS_VOICE)),
        )


@dataclass
class Config:
    language: str = "en"
    planner: RoleConfig = field(default_factory=lambda: RoleConfig(cli="opencode"))
    implementer: RoleConfig = field(default_factory=lambda: RoleConfig(cli="kimi"))
    reviewer: RoleConfig = field(default_factory=lambda: RoleConfig(cli="opencode"))
    final: RoleConfig = field(default_factory=lambda: RoleConfig(cli="opencode"))
    automode: AutomodeConfig = field(default_factory=AutomodeConfig)
    hook: HookConfig = field(default_factory=HookConfig)
    editor: EditorConfig = field(default_factory=EditorConfig)
    final_prompt: str = ""  # extra instructions for the final-steps phase
    theme: str = ""  # Textual palette; empty = default theme
    auto_update: bool = False  # update agent CLIs on TUI startup (native commands)
    workspaces: list[str] = field(default_factory=list)  # root folders whose subfolders are projects
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    def role(self, name: str) -> RoleConfig:
        return getattr(self, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "planner": self.planner.to_dict(),
            "implementer": self.implementer.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "final": self.final.to_dict(),
            "automode": self.automode.to_dict(),
            "hook": self.hook.to_dict(),
            "editor": self.editor.to_dict(),
            "final_prompt": self.final_prompt,
            "theme": self.theme,
            "auto_update": self.auto_update,
            "workspaces": list(self.workspaces),
            "telegram": self.telegram.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        return cls(
            language=str(data.get("language", "en")),
            planner=RoleConfig.from_dict(data.get("planner", {}), default_cli="opencode"),
            implementer=RoleConfig.from_dict(data.get("implementer", {}), default_cli="kimi"),
            reviewer=RoleConfig.from_dict(data.get("reviewer", {}), default_cli="opencode"),
            final=RoleConfig.from_dict(data.get("final", {}), default_cli="opencode"),
            automode=AutomodeConfig.from_dict(data.get("automode", {})),
            hook=HookConfig.from_dict(data.get("hook", {})),
            editor=EditorConfig.from_dict(data.get("editor", {})),
            final_prompt=str(data.get("final_prompt", "")),
            theme=str(data.get("theme", "")),
            auto_update=bool(data.get("auto_update", False)),
            workspaces=[
                str(item)
                for item in data.get("workspaces", [])
                if isinstance(item, str)
            ] if isinstance(data.get("workspaces", []), list) else [],
            telegram=TelegramConfig.from_dict(data.get("telegram", {})),
        )


def load() -> Config:
    path = paths.config_path()
    if not path.exists():
        config = Config()
        save(config)
        return config
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return Config.from_dict(data)


def save(config: Config) -> None:
    paths.config_path().write_text(_toml.dumps(config.to_dict()), encoding="utf-8")


def _project_overrides(workdir: Path) -> dict[str, Any]:
    """Read ``<workdir>/.grafeno.toml`` and return its ``[editor]`` section or ``{}``."""
    path = workdir / PROJECT_CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("editor", {})
    return section if isinstance(section, dict) else {}


def resolve_editor_config(config: Config, workdir: Path | None) -> EditorConfig:
    """Effective editor config: the global one, overridden by the project's
    ``.grafeno.toml`` if it exists (only the ``[editor]`` section)."""
    if workdir is None:
        return config.editor
    overrides = _project_overrides(workdir)
    if not overrides:
        return config.editor
    merged = config.editor.to_dict() | overrides
    return EditorConfig.from_dict(merged)
