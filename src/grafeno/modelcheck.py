"""Verify that configured models are still offered by their CLIs.

CLIs and subscriptions drop models over time, and a stale ``cli+model``
pair makes the pipeline fail mid-task with a provider error. The check is
pure logic: the caller fetches each CLI's model list once
(``drivers.fetch_all_models``) and ``find_missing`` compares every
configured pair against it. A CLI whose list could not be fetched (not
installed, command failed) is skipped: an empty list is not proof of a
removed model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from .config import Config, RoleConfig
from .i18n import t
from .models import cli_model_label
from .profiles import Profile

ROLE_NAMES = ("first", "planner", "implementer", "reviewer", "final")

# Role name -> phase i18n key (roles share names with the pipeline phases).
_ROLE_PHASE_KEY = {
    "first": "phase.first",
    "planner": "phase.plan",
    "implementer": "phase.implement",
    "reviewer": "phase.review",
    "final": "phase.final",
}


class _RolesHolder(Protocol):
    """Anything exposing the five pipeline roles (Config, Profile, Task)."""

    def role(self, name: str) -> RoleConfig: ...


@dataclass
class ModelIssue:
    """A ``cli+model`` pair no longer listed by its CLI."""

    role: str       # pipeline role name, or "telegram_parser"
    cli: str
    model: str
    context: str = ""  # profile name; "" = general config / the task itself

    @property
    def role_label(self) -> str:
        """Translated, human-readable role name."""
        if self.role == "telegram_parser":
            return t("modelcheck.role.parser")
        return t(_ROLE_PHASE_KEY.get(self.role, "phase.plan"))


def role_items(holder: _RolesHolder) -> list[tuple[str, RoleConfig]]:
    """``(role name, RoleConfig)`` for the five pipeline roles of ``holder``."""
    return [(name, holder.role(name)) for name in ROLE_NAMES]


def collect_config_roles(cfg: Config) -> list[ModelIssue]:
    """Candidate pairs of the general config (roles + Telegram parser).

    They are returned as ``ModelIssue`` shells (the check has not run yet):
    ``find_missing`` filters the ones actually gone. The Telegram parser is
    only included when it pins an explicit model; otherwise it inherits the
    planner role, already covered.
    """
    pairs = [
        ModelIssue(role=name, cli=role.cli, model=role.model)
        for name, role in role_items(cfg)
    ]
    parser_model = cfg.telegram.parser_model.strip()
    if parser_model:
        pairs.append(ModelIssue(
            role="telegram_parser",
            cli=cfg.telegram.parser_cli.strip() or cfg.planner.cli,
            model=parser_model,
        ))
    return pairs


def collect_profile_roles(profiles: Iterable[Profile]) -> list[ModelIssue]:
    """Candidate pairs of every processing profile (context = profile name)."""
    pairs: list[ModelIssue] = []
    for profile in profiles:
        for name, role in role_items(profile):
            pairs.append(ModelIssue(
                role=name, cli=role.cli, model=role.model, context=profile.name,
            ))
    return pairs


def collect_task_roles(task: _RolesHolder) -> list[ModelIssue]:
    """Candidate pairs of a task (its snapshot of the role assignments)."""
    return [
        ModelIssue(role=name, cli=role.cli, model=role.model)
        for name, role in role_items(task)
    ]


def used_clis(pairs: Iterable[ModelIssue]) -> set[str]:
    """CLIs with at least one explicit model (those are the checkable ones)."""
    return {pair.cli.strip() for pair in pairs if pair.model.strip() and pair.cli.strip()}


def find_missing(
    pairs: Iterable[ModelIssue],
    available: dict[str, list[str]],
) -> list[ModelIssue]:
    """Pairs whose model is NOT in the list reported by its CLI.

    Skipped (never an issue): empty model (CLI default), blank CLI, or a
    CLI without a fetched list (missing key or empty list: a failed fetch
    is not proof of a removed model).
    """
    issues: list[ModelIssue] = []
    for pair in pairs:
        model = pair.model.strip()
        cli = pair.cli.strip()
        if not model or not cli or not available.get(cli):
            continue
        if model not in available[cli]:
            issues.append(ModelIssue(pair.role, cli, model, pair.context))
    return issues


def format_issues(issues: Iterable[ModelIssue]) -> list[str]:
    """One sentence per unique ``(cli, model, context)``, roles joined.

    Grouping avoids alarming twice when two roles share the same removed
    model (e.g. planner and reviewer). First-seen order is kept.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for issue in issues:
        key = (issue.cli, issue.model, issue.context)
        groups.setdefault(key, [])
        if issue.role_label not in groups[key]:
            groups[key].append(issue.role_label)
    lines: list[str] = []
    for (cli, model, context), labels in groups.items():
        pair = cli_model_label(cli, model)
        roles = ", ".join(labels)
        if context:
            lines.append(t("modelcheck.missing_profile", roles=roles, pair=pair, profile=context))
        else:
            lines.append(t("modelcheck.missing", roles=roles, pair=pair))
    return lines
