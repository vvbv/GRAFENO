"""Git operations on the target project (branch per task). Never raises.

Read-only helpers (``current_head``, ``changes_markdown``) provide the diff
base and the final changes report without mutating the repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..i18n import t

# Cap the embedded diff to keep the report light on token-heavy runs.
_MAX_DIFF_BYTES = 512 * 1024
# Cap each embedded untracked file so the report cannot explode on a stray
# blob (binary content is also skipped; see ``_read_capped``).
_MAX_FILE_BYTES = 64 * 1024
_BINARY_SCAN_BYTES = 1024
_MAX_UNTRACKED_FILES = 20


def _git(workdir: Path, *args: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(workdir), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout + completed.stderr).strip()
    return completed.returncode == 0, output


def is_git_repo(workdir: Path) -> bool:
    ok, output = _git(workdir, "rev-parse", "--is-inside-work-tree")
    return ok and output.strip() == "true"


def create_branch(workdir: Path, branch: str) -> tuple[bool, str]:
    """Create (or switch to) the task branch. Returns (success, message)."""
    ok, output = _git(workdir, "checkout", "-b", branch)
    if ok:
        return True, t("git.branch_created", branch=branch)
    if "already exists" in output:
        ok, output = _git(workdir, "checkout", branch)
        if ok:
            return True, t("git.branch_exists", branch=branch)
    return False, output or t("git.branch_failed", branch=branch)


def current_head(workdir: Path) -> str:
    """Current HEAD commit hash, or "" when unavailable (not a repo, etc.)."""
    ok, output = _git(workdir, "rev-parse", "HEAD")
    return output.splitlines()[0].strip() if ok and output.strip() else ""


def _resolve_base(workdir: Path, base: str) -> tuple[str, bool]:
    """Return ``(effective_base, is_fallback)``; empty when no valid base."""
    if base:
        ok, _ = _git(workdir, "rev-parse", "--verify", f"{base}^{{commit}}")
        if ok:
            return base, False
    head = current_head(workdir)
    return head, bool(head)


def _read_capped(path: Path, limit: int) -> str | None:
    """File content up to ``limit`` bytes; ``None`` for binary/unreadable/too large."""
    try:
        with path.open("rb") as handle:
            sample = handle.read(_BINARY_SCAN_BYTES)
            if b"\0" in sample:
                return None
            handle.seek(0)
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data.decode("utf-8", errors="replace")


def _untracked_section(workdir: Path) -> tuple[str, list[str]]:
    """Return ``(markdown, skipped)`` for untracked new files."""
    ok, listing = _git(workdir, "ls-files", "--others", "--exclude-standard")
    if not ok:
        return "(ninguno)", []
    files = sorted(line.strip() for line in listing.splitlines() if line.strip())
    if not files:
        return "(ninguno)", []
    blocks: list[str] = []
    skipped: list[str] = []
    included = 0
    for rel in files:
        if included >= _MAX_UNTRACKED_FILES:
            skipped.append(rel)
            continue
        content = _read_capped(workdir / rel, _MAX_FILE_BYTES)
        if content is None:
            skipped.append(rel)
            continue
        blocks.append(f"### {rel}\n```\n{content}\n```")
        included += 1
    body_parts: list[str] = []
    body_parts.extend(blocks)
    body_parts.extend(f"- {rel} (omitido: binario o demasiado grande)" for rel in skipped)
    if not body_parts:
        return "(ninguno)", []
    return "\n".join(body_parts), skipped


def changes_markdown(workdir: Path, base: str) -> str:
    """Build the Markdown report of every change contributed by the task.

    The document lists commits, working-tree status, the full diff against
    ``base`` and the contents of any new untracked files. Returns ``""``
    when no report can be produced (not a repo, no commits at all). The
    helper is read-only and never raises.
    """
    if not is_git_repo(workdir):
        return ""
    effective, is_fallback = _resolve_base(workdir, base)
    if not effective:
        return ""

    _, commits = _git(workdir, "log", "--oneline", f"{effective}..HEAD")
    _, status = _git(workdir, "status", "--porcelain")
    diff_ok, diff_text = _git(workdir, "diff", effective)
    if diff_ok and len(diff_text.encode("utf-8")) > _MAX_DIFF_BYTES:
        diff_text = diff_text.encode("utf-8")[:_MAX_DIFF_BYTES].decode("utf-8", errors="replace")
        diff_text = f"{diff_text}\n... (diff truncated)"
    untracked_md, _ = _untracked_section(workdir)

    short_base = effective[:7]
    fallback_notice = ""
    if is_fallback:
        fallback_notice = (
            f"\n\n> Aviso: la base original no estaba disponible; se usa HEAD "
            f"(`{short_base}`) y solo se documentan los cambios sin "
            f"comitear a partir de ese punto."
        )

    commits_section = commits or "(ninguno)"
    status_section = status or "(limpio)"
    diff_section = diff_text or "(sin cambios)"

    parts = [
        "# Cambios de la tarea",
        f"Cambios introducidos por la tarea desde `{short_base}` "
        f"(comiteados y sin comitear).{fallback_notice}",
        "## Commits",
        commits_section,
        "## Estado del arbol de trabajo",
        status_section,
        "## Diff",
        "```diff",
        diff_section,
        "```",
        "## Archivos nuevos sin seguimiento",
        untracked_md,
    ]
    return "\n".join(parts) + "\n"
