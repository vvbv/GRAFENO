"""Tests of root workspaces: project-level loading, resolution and discovery."""

from __future__ import annotations

import os
from pathlib import Path

from grafeno import workspaces
from grafeno.config import PROJECT_CONFIG_FILE


def test_discover_first_level_subdirs(tmp_path):
    """Only first-level directories count; files and hidden folders are skipped."""
    ws = tmp_path / "ws"
    (ws / "proyecto-a" / "anidado").mkdir(parents=True)
    (ws / "proyecto-b").mkdir()
    (ws / ".hidden").mkdir()
    (ws / "README.md").write_text("x", encoding="utf-8")
    assert workspaces.discover([ws]) == [ws / "proyecto-a", ws / "proyecto-b"]


def test_discover_dedupes_overlapping_workspaces(tmp_path):
    """The same folder reached from two workspaces appears only once."""
    ws = tmp_path / "ws"
    (ws / "proyecto").mkdir(parents=True)
    result = workspaces.discover([ws, Path(str(ws) + os.sep)])
    assert result == [ws / "proyecto"]


def test_discover_ignores_missing_workspace(tmp_path):
    """A workspace pointing nowhere yields no projects and no exception."""
    assert workspaces.discover([tmp_path / "no-existe"]) == []


def test_resolve_expands_and_filters(tmp_path):
    """Duplicated and non-existing entries are dropped; results are resolved."""
    ws = tmp_path / "ws"
    ws.mkdir()
    result = workspaces.resolve([str(ws), str(tmp_path / "no-existe"), str(ws)])
    assert result == [ws.resolve()]
    assert result[0] == (tmp_path / "ws").resolve()


def test_resolve_merges_project_level(tmp_path):
    """Project-level workspaces are appended after the global ones."""
    global_ws = tmp_path / "global-ws"
    global_ws.mkdir()
    project_ws = tmp_path / "project-ws"
    project_ws.mkdir()
    project = tmp_path / "proyecto"
    project.mkdir()
    (project / PROJECT_CONFIG_FILE).write_text(
        f'workspaces = ["{project_ws}"]\n', encoding="utf-8"
    )
    assert workspaces.resolve([str(global_ws)], project) == [
        global_ws.resolve(),
        project_ws.resolve(),
    ]


def test_load_project_tolerates_missing_and_invalid(tmp_path):
    """Missing file, bare string and corrupt TOML never raise."""
    project = tmp_path / "proyecto"
    project.mkdir()
    assert workspaces.load_project(project) == []
    (project / PROJECT_CONFIG_FILE).write_text(
        'workspaces = "una-ruta"\n', encoding="utf-8"
    )
    assert workspaces.load_project(project) == ["una-ruta"]
    (project / PROJECT_CONFIG_FILE).write_text("[", encoding="utf-8")
    assert workspaces.load_project(project) == []


def test_projects_keep_workspace_and_sort_case_insensitive(tmp_path):
    """Projects carry their root and are sorted by name ignoring case."""
    ws = tmp_path / "ws"
    for name in ("beta", "Alpha", "gamma"):
        (ws / name).mkdir(parents=True)
    (ws / ".hidden").mkdir()
    result = workspaces.projects([ws])
    assert [project.name for project in result] == ["Alpha", "beta", "gamma"]
    assert all(project.workspace == ws for project in result)
    assert result[0].path == ws / "Alpha"
    assert workspaces.discover([ws]) == [ws / "Alpha", ws / "beta", ws / "gamma"]


def test_roots_report_state_and_dedupe(tmp_path):
    """Roots keep the configured text, resolve it and flag missing folders."""
    ws = tmp_path / "ws"
    ws.mkdir()
    missing = tmp_path / "missing"
    result = workspaces.roots([str(ws), "  ", str(missing), str(ws) + os.sep])
    assert [root.entry for root in result] == [str(ws), str(missing)]
    assert result[0].path == str(ws.resolve())
    assert result[0].exists is True
    assert result[1].exists is False


def test_roots_expand_user(tmp_path, monkeypatch):
    """``~`` entries are expanded but the original text is kept as entry."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "code").mkdir()
    [root] = workspaces.roots(["~/code"])
    assert root.entry == "~/code"
    assert root.path == str((tmp_path / "code").resolve())
    assert root.exists is True


def test_effective_entries_append_project_level(tmp_path):
    """Project-level entries come after the global ones."""
    project = tmp_path / "proyecto"
    project.mkdir()
    (project / PROJECT_CONFIG_FILE).write_text('workspaces = ["/srv/extra"]\n', encoding="utf-8")
    assert workspaces.effective_entries(["~/code"], project) == ["~/code", "/srv/extra"]
    assert workspaces.effective_entries(["~/code"]) == ["~/code"]


def test_normalize_entries_strips_and_dedupes():
    """Blank entries go away; trailing separators and duplicates are folded."""
    entries = ["  ~/code/ ", "~/code", "", "   ", "/srv/app//", "/srv/app"]
    assert workspaces.normalize_entries(entries) == ["~/code", "/srv/app"]
    assert workspaces.entry_key("~/code/") == workspaces.entry_key("~/code")


def test_resolve_ignores_blank_entries(tmp_path, monkeypatch):
    """A blank entry no longer resolves to the current directory."""
    monkeypatch.chdir(tmp_path)
    assert workspaces.resolve(["", "   "]) == []
