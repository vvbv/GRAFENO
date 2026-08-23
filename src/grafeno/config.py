"""Configuración global de GRAFENO (~/.grafeno/config.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import _toml, paths

KNOWN_CLIS = ("opencode", "kimi")  # futuro: "codex", "claude"


@dataclass
class RoleConfig:
    """CLI + modelo asignados a un rol del pipeline."""

    cli: str = "opencode"
    model: str = ""  # vacío = modelo por defecto del CLI

    def to_dict(self) -> dict[str, Any]:
        return {"cli": self.cli, "model": self.model}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, default_cli: str) -> "RoleConfig":
        return cls(
            cli=str(data.get("cli", default_cli)),
            model=str(data.get("model", "")),
        )


@dataclass
class AutomodeConfig:
    enabled: bool = False
    max_iterations: int = 5
    test_command: str = ""  # vacío = sin tests
    create_branch: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_iterations": self.max_iterations,
            "test_command": self.test_command,
            "create_branch": self.create_branch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AutomodeConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            max_iterations=int(data.get("max_iterations", 5)),
            test_command=str(data.get("test_command", "")),
            create_branch=bool(data.get("create_branch", True)),
        )


@dataclass
class Config:
    planner: RoleConfig = field(default_factory=lambda: RoleConfig(cli="opencode"))
    implementer: RoleConfig = field(default_factory=lambda: RoleConfig(cli="kimi"))
    reviewer: RoleConfig = field(default_factory=lambda: RoleConfig(cli="opencode"))
    automode: AutomodeConfig = field(default_factory=AutomodeConfig)

    def role(self, name: str) -> RoleConfig:
        return getattr(self, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "planner": self.planner.to_dict(),
            "implementer": self.implementer.to_dict(),
            "reviewer": self.reviewer.to_dict(),
            "automode": self.automode.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        return cls(
            planner=RoleConfig.from_dict(data.get("planner", {}), default_cli="opencode"),
            implementer=RoleConfig.from_dict(data.get("implementer", {}), default_cli="kimi"),
            reviewer=RoleConfig.from_dict(data.get("reviewer", {}), default_cli="opencode"),
            automode=AutomodeConfig.from_dict(data.get("automode", {})),
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
