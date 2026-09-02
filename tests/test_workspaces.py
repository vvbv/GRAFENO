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
