"""Processing profiles: named role assignments (CLI + model + effort).

A profile groups the five pipeline roles (first/planner/implementer/
reviewer/final) under a user-chosen name. Profiles live in the global file
``~/.grafeno/profiles.toml`` and are selected per task at creation time:
the task snapshots the profile roles, overriding the global ``Config``
roles for that task only.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from typing import Any

from . import _toml, paths
from .config import RoleConfig

PROFILE_ROLES = ("first", "planner", "implementer", "reviewer", "final")


@dataclass
class Profile:
    """A named set of role assignments (CLI + model + effort per role)."""

    name: str = ""
    first: RoleConfig = field(default_factory=RoleConfig)
    planner: RoleConfig = field(default_factory=RoleConfig)
    implementer: RoleConfig = field(default_factory=RoleConfig)
    reviewer: RoleConfig = field(default_factory=RoleConfig)
    final: RoleConfig = field(default_factory=RoleConfig)

    def role(self, name: str) -> RoleConfig:
        return getattr(self, name)

    def summary(self) -> str:
        """Compact ``cli/model`` per role, e.g. ``plan=opencode/k3 · ...``."""
        tags = {
            "first": "first",
            "planner": "plan",
            "implementer": "impl",
            "reviewer": "rev",
            "final": "final",
        }
        parts = []
        for role in PROFILE_ROLES:
            cfg = self.role(role)
            parts.append(f"{tags[role]}={cfg.cli}/{cfg.model or 'default'}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        """Flat mapping (``planner_cli``...): the TOML writer is scalar-only."""
        data: dict[str, Any] = {"name": self.name}
        for role in PROFILE_ROLES:
            cfg = self.role(role)
            data[f"{role}_cli"] = cfg.cli
            data[f"{role}_model"] = cfg.model
            data[f"{role}_effort"] = cfg.effort
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        profile = cls(name=str(data.get("name", "")))
        for role in PROFILE_ROLES:
            setattr(profile, role, RoleConfig(
                cli=str(data.get(f"{role}_cli", "opencode")),
                model=str(data.get(f"{role}_model", "")),
                effort=str(data.get(f"{role}_effort", "")),
            ))
        return profile


def load_global() -> list[Profile]:
    """Profiles from ``~/.grafeno/profiles.toml`` (missing/corrupt = [])."""
    path = paths.profiles_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    raw = data.get("profiles", [])
    if not isinstance(raw, list):
        return []
    return [Profile.from_dict(item) for item in raw if isinstance(item, dict)]


def save_global(profiles: list[Profile]) -> None:
    """Write the global profiles file."""
    payload = {"profiles": [profile.to_dict() for profile in profiles]}
    paths.profiles_path().write_text(_toml.dumps(payload), encoding="utf-8")


def find(name: str, profiles: list[Profile] | None = None) -> Profile | None:
    """First profile named ``name`` (loads the global list when omitted)."""
    if not name.strip():
        return None
    for profile in (load_global() if profiles is None else profiles):
        if profile.name == name:
            return profile
    return None