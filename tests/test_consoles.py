"""Tests of the consoles module and its per-project persistence."""

from __future__ import annotations

from grafeno import _toml
from grafeno.config import PROJECT_CONFIG_FILE
from grafeno.consoles import (
    CONSOLE_COLORS,
    ConsoleSpec,
    default_shell,
    external_terminal_command,
    load_project,
    open_external_terminal,
    save_project,
    supported,
)


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


def test_external_terminal_command_appends_shell(monkeypatch, tmp_path):
    """-e style terminals get the shell appended after the template."""
    from grafeno import editor

    monkeypatch.setattr(
        editor,
        "detect_terminal",
        lambda: editor.TerminalInfo(name="alacritty", window_command=["alacritty", "-e"]),
    )
    command = external_terminal_command(str(tmp_path))
    assert command[:2] == ["alacritty", "-e"]
    assert len(command) == 3  # shell path appended


def test_external_terminal_command_tmux_gets_workdir_flag(monkeypatch, tmp_path):
    """tmux needs an explicit -c with the workdir (it does not inherit cwd)."""
    from grafeno import editor

    monkeypatch.setattr(
        editor,
        "detect_terminal",
        lambda: editor.TerminalInfo(name="tmux", window_command=["tmux", "new-window"]),
    )
    assert external_terminal_command(str(tmp_path)) == ["tmux", "new-window", "-c", str(tmp_path)]


def test_external_terminal_command_unknown_terminal_returns_none(monkeypatch, tmp_path):
    """An unknown terminal yields None (best effort)."""
    from grafeno import editor

    monkeypatch.setattr(editor, "detect_terminal", lambda: editor.TerminalInfo())
    assert external_terminal_command(str(tmp_path)) is None


def test_open_external_terminal_runs_command_in_workdir(monkeypatch, tmp_path):
    """open_external_terminal spawns the detected command with cwd=workdir."""
    from grafeno import editor
    import grafeno.consoles as consoles_module

    monkeypatch.setattr(
        editor,
        "detect_terminal",
        lambda: editor.TerminalInfo(name="alacritty", window_command=["alacritty", "-e"]),
    )
    calls: dict[str, object] = {}

    class FakePopen:
        def __init__(self, command, cwd=None, stdout=None, stderr=None):
            calls["command"] = command
            calls["cwd"] = cwd

    monkeypatch.setattr(consoles_module.subprocess, "Popen", FakePopen)
    assert open_external_terminal(str(tmp_path)) is True
    assert calls["cwd"] == str(tmp_path)


def test_open_external_terminal_without_terminal_returns_false(monkeypatch, tmp_path):
    """Without a detected terminal nothing is spawned and False is returned."""
    from grafeno import editor

    monkeypatch.setattr(editor, "detect_terminal", lambda: editor.TerminalInfo())
    assert open_external_terminal(str(tmp_path)) is False
