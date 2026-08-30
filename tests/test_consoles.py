"""Tests of the consoles module and its per-project persistence."""

from __future__ import annotations

from grafeno import _toml
from grafeno.config import PROJECT_CONFIG_FILE
from grafeno.consoles import CONSOLE_COLORS, ConsoleSpec, default_shell, load_project, save_project, supported


def test_console_spec_dict_roundtrip():
    """ConsoleSpec roundtrips through to_dict/from_dict."""
    spec = ConsoleSpec(name="server", command="npm run dev", color="green")
    assert ConsoleSpec.from_dict(spec.to_dict()) == spec


def test_console_spec_from_dict_defaults():
    """Missing keys default to empty strings."""
    assert ConsoleSpec.from_dict({}) == ConsoleSpec(name="", command="", color="")


def test_console_spec_invalid_color_falls_back_to_default():
    """A hand-edited unknown color is normalized to the default."""
    spec = ConsoleSpec.from_dict({"name": "x", "color": "chartreuse"})
    assert spec.color == ""
    assert "" in CONSOLE_COLORS and "cyan" in CONSOLE_COLORS


def test_load_project_missing_returns_empty(tmp_path):
    """If the project config does not exist, returns []."""
    assert load_project(tmp_path) == []


def test_load_project_with_corrupt_file_returns_empty(tmp_path):
    """A corrupt .grafeno.toml yields an empty list (tolerant pattern)."""
    (tmp_path / PROJECT_CONFIG_FILE).write_text("not valid toml [[[", encoding="utf-8")
    assert load_project(tmp_path) == []


def test_save_load_project_roundtrip(tmp_path):
    """Saving and reloading returns the same list of consoles."""
    specs = [
        ConsoleSpec(name="shell"),
        ConsoleSpec(name="tests", command="pytest -x", color="yellow"),
    ]
    save_project(tmp_path, specs)
    assert load_project(tmp_path) == specs


def test_save_project_preserves_other_sections(tmp_path):
    """Saving consoles keeps [editor] and [[references]] sections intact."""
    payload = {
        "editor": {"enabled": True, "editor": "code", "mode": "window", "side": "left"},
        "references": [{"name": "p1", "description": "", "path": "/p1"}],
    }
    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps(payload), encoding="utf-8")
    save_project(tmp_path, [ConsoleSpec(name="shell", color="red")])
    import tomllib
    with (tmp_path / PROJECT_CONFIG_FILE).open("rb") as handle:
        data = tomllib.load(handle)
    assert data["editor"]["editor"] == "code"
    assert data["references"] == [{"name": "p1", "description": "", "path": "/p1"}]
    assert data["consoles"] == [{"name": "shell", "command": "", "color": "red"}]


def test_save_project_ignores_unsupported_values(tmp_path):
    """A hand-edited array of scalars is dropped instead of breaking the save."""
    (tmp_path / PROJECT_CONFIG_FILE).write_text('tags = ["a", "b"]\n', encoding="utf-8")
    save_project(tmp_path, [ConsoleSpec(name="shell")])
    assert load_project(tmp_path) == [ConsoleSpec(name="shell")]


def test_supported_matches_platform_and_shell_is_non_empty():
    """supported() reflects the platform and the default shell is a path."""
    import os
    assert supported() == (os.name == "posix")
    assert default_shell()
