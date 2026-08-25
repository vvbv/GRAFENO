"""Global GRAFENO configuration (~/.grafeno/config.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import _toml, paths

KNOWN_CLIS = ("opencode", "kimi", "codex", "claude")
PROJECT_CONFIG_FILE = ".grafeno.toml"


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
