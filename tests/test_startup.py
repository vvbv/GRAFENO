"""Tests for the startup commands module."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from grafeno import paths, startup
from grafeno.config import StartupAction
from grafeno.startup import StartupRunner

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")


def _sleep_action(**kwargs) -> StartupAction:
    return StartupAction(name="sleeper", command=f'{sys.executable} -c "import time; time.sleep(30)"', **kwargs)


def test_parse_settings_accepts_object_and_empty():
    assert startup.parse_settings("") == {}
    assert startup.parse_settings('{"a": 1}') == {"a": 1}


@pytest.mark.parametrize("text", ["[1]", "nope"])
def test_parse_settings_rejects_non_object(text):
    with pytest.raises(ValueError):
        startup.parse_settings(text)


def test_strip_jsonc_comments_and_trailing_commas():
    text = (
        '{\n  // c\n  "a": 1, /* b */\n  "url": "http://x/*y*/",\n'
        '  "list": [1, 2,],\n  "z": 2, // c\n}'
    )
    assert json.loads(startup.strip_jsonc(text)) == {
        "a": 1, "url": "http://x/*y*/", "list": [1, 2], "z": 2,
    }


def test_apply_settings_creates_file_and_parents(tmp_path):
    path = tmp_path / "a/b/settings.json"
    assert startup.apply_settings(str(path), {"git.repositoryScanMaxDepth": -1}) is True
    assert json.loads(path.read_text())["git.repositoryScanMaxDepth"] == -1


def test_apply_settings_merges_and_is_idempotent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"editor.fontSize": 14, "git.repositoryScanMaxDepth": 1}))
    assert startup.apply_settings(str(path), {"git.repositoryScanMaxDepth": -1}) is True
    data = json.loads(path.read_text())
    assert data == {"editor.fontSize": 14, "git.repositoryScanMaxDepth": -1}
    assert startup.apply_settings(str(path), {"git.repositoryScanMaxDepth": -1}) is False


def test_apply_settings_jsonc_keeps_backup(tmp_path):
    path = tmp_path / "settings.json"
    original = '{\n  // note\n  "a": 1,\n}\n'
    path.write_text(original)
    assert startup.apply_settings(str(path), {"b": 2}) is True
    assert json.loads(path.read_text()) == {"a": 1, "b": 2}
    assert (tmp_path / "settings.json.grafeno.bak").read_text() == original


def test_apply_settings_never_overwrites_invalid_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    with pytest.raises(ValueError):
        startup.apply_settings(str(path), {"a": 1})
    assert path.read_text() == "{ not json"


def test_apply_settings_bool_is_not_int(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"x": true}')
    assert startup.apply_settings(str(path), {"x": 1}) is True
    assert json.loads(path.read_text()) == {"x": 1}


def test_build_argv_substitutes_workdir_and_resolves(monkeypatch):
    monkeypatch.setattr(startup.shutil, "which", lambda name: "/usr/bin/" + name)
    action = StartupAction(command="code serve-web --default-folder {workdir}")
    assert startup.build_argv(action, "/w") == ["/usr/bin/code", "serve-web", "--default-folder", "/w"]


def test_build_argv_missing_executable(monkeypatch):
    monkeypatch.setattr(startup.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        startup.build_argv(StartupAction(command="zz"), "/w")
    with pytest.raises(ValueError):
        startup.build_argv(StartupAction(command="  "), "/w")


def test_presets_are_copies_and_enforce_git_depth():
    chosen = startup.preset("vscode")
    assert chosen.command == "code serve-web --host 0.0.0.0 --port 8080 --without-connection-token"
    assert startup.parse_settings(chosen.settings) == {"workbench.colorTheme": "Default Dark Modern", "git.repositoryScanMaxDepth": -1}
    chosen.command = "changed"
    assert startup.PRESETS["vscode"].command != "changed"
    assert startup.preset("nope") is None
    for key in startup.PRESETS:
        assert startup.parse_settings(startup.PRESETS[key].settings)["git.repositoryScanMaxDepth"] == -1
        assert startup.parse_settings(startup.PRESETS[key].settings)["workbench.colorTheme"] == "Default Dark Modern"


def test_validation_error():
    ok = StartupAction(name="a", command="b")
    assert startup.validation_error(ok) == ""
    assert "required" in startup.validation_error(StartupAction(command="b"))
    assert "JSON object" in startup.validation_error(StartupAction(name="a", command="b", settings="[1]"))
    assert "settings file" in startup.validation_error(StartupAction(name="a", command="b", settings="{}"))


@posix_only
def test_runner_launch_and_stop(tmp_path):
    runner = StartupRunner()
    outcomes = runner.launch_all([_sleep_action()], str(tmp_path))
    assert outcomes[0].ok
    process = runner.processes[0].process
    assert process.poll() is None
    started = time.monotonic()
    runner.stop_all()
    assert process.poll() is not None
    assert time.monotonic() - started < startup.STOP_GRACE_SECONDS + 2


@posix_only
def test_runner_keeps_process_without_stop_on_exit(tmp_path):
    runner = StartupRunner()
    runner.launch_all([_sleep_action(stop_on_exit=False)], str(tmp_path))
    process = runner.processes[0].process
    runner.stop_all()
    try:
        assert process.poll() is None
    finally:
        process.kill()
        process.wait()


def test_runner_reports_early_exit_once(tmp_path):
    runner = StartupRunner()
    action = StartupAction(name="boom", command=f'{sys.executable} -c "import sys; sys.exit(3)"')
    runner.launch_all([action], str(tmp_path))
    item = runner.processes[0]
    item.process.wait(timeout=10)
    assert runner.early_exits() == [(item, 3)]
    assert runner.early_exits() == []
    assert (paths.startup_dir() / "boom.log").exists()


def test_runner_skips_disabled_and_reports_failures(tmp_path):
    runner = StartupRunner()
    outcomes = runner.launch_all([
        StartupAction(name="off", command="x", enabled=False),
        StartupAction(name="missing", command="definitely-not-a-command-zz"),
        StartupAction(name="nodir", command="x", workdir=str(tmp_path / "nope")),
    ], str(tmp_path))
    assert [o.name for o in outcomes] == ["missing", "nodir"]
    assert not outcomes[0].ok and "command not found" in outcomes[0].detail
    assert not outcomes[1].ok


def test_runner_settings_error_does_not_block_launch(tmp_path):
    runner = StartupRunner()
    action = StartupAction(
        name="s", command=f'{sys.executable} -c "import time; time.sleep(30)"',
        settings="[1]", settings_file=str(tmp_path / "s.json"),
    )
    try:
        outcomes = runner.launch_all([action], str(tmp_path))
        assert outcomes[0].ok and outcomes[0].settings_error
    finally:
        runner.stop_all()


def test_presets_are_editors():
    for item in startup.PRESETS.values():
        assert item.open_url == startup.EDITOR_URL
        assert startup.parse_settings(item.settings) == {**startup.EDITOR_DEFAULTS, **startup.EDITOR_SETTINGS}


def test_effective_settings_force_editor_value():
    text = '{"git.repositoryScanMaxDepth": 1, "a": 2}'
    editor = StartupAction(name="e", command="c", settings=text, open_url="http://{folder}")
    assert startup.effective_settings(editor) == {"workbench.colorTheme": "Default Dark Modern", "git.repositoryScanMaxDepth": -1, "a": 2}
    assert startup.effective_settings(StartupAction(name="e", command="c", settings=text))["git.repositoryScanMaxDepth"] == 1
    assert "workbench.colorTheme" not in startup.effective_settings(StartupAction(name="e", command="c", settings=text))


def test_effective_settings_user_theme_wins():
    text = '{"workbench.colorTheme": "Default Light Modern", "git.repositoryScanMaxDepth": 3}'
    editor = StartupAction(name="e", command="c", settings=text, open_url="http://{folder}")
    assert startup.effective_settings(editor) == {
        "workbench.colorTheme": "Default Light Modern", "git.repositoryScanMaxDepth": -1,
    }


def test_effective_settings_dark_theme_for_saved_editors():
    editor = StartupAction(name="e", command="c", settings="", open_url="http://{folder}")
    assert startup.effective_settings(editor) == {
        "workbench.colorTheme": "Default Dark Modern", "git.repositoryScanMaxDepth": -1,
    }


def test_editor_action_selection():
    assert startup.editor_action([]) is None
    plain = StartupAction(name="p", command="c")
    off = StartupAction(name="off", command="c", open_url="http://{folder}", enabled=False)
    on = StartupAction(name="on", command="c", open_url="http://{folder}")
    assert startup.editor_action([plain]) is None
    assert startup.editor_action([plain, off, on]) is on
    assert startup.editor_action([plain, off]) is off


def test_editor_url_and_hosts():
    action = StartupAction(open_url="http://{host}:8080/?folder={folder}&n={name}")
    assert startup.editor_url(action, "/home/u/My Proj", "10.0.0.5") == (
        "http://10.0.0.5:8080/?folder=/home/u/My%20Proj&n=My%20Proj")
    assert startup.editor_url(action, "/x", "").startswith("http://localhost:8080")
    assert startup.normalize_host("::1") == "[::1]"
    assert startup.normalize_host("[::1]") == "[::1]"
    with pytest.raises(ValueError):
        startup.normalize_host("evil/x")
    with pytest.raises(ValueError):
        startup.editor_url(StartupAction(open_url="javascript:{folder}"), "/x")
    assert startup.folder_for_url("C:\\code\\x") == "/C:/code/x"


def test_open_url_validation():
    def check(url):
        return startup.validation_error(StartupAction(name="a", command="b", open_url=url))

    assert check("ftp://x/{folder}")
    assert check("http://x/")
    assert check("http://{host}:8080/?folder={folder}") == ""


def test_launch_enforces_editor_settings(tmp_path):
    target = tmp_path / "s.json"
    runner = StartupRunner()
    action = _sleep_action(settings_file=str(target), open_url="http://{host}:8080/?folder={folder}")
    try:
        assert runner.launch(action, str(tmp_path)).ok
        assert json.loads(target.read_text())["git.repositoryScanMaxDepth"] == -1
        assert json.loads(target.read_text())["workbench.colorTheme"] == "Default Dark Modern"
    finally:
        runner.stop_all()
