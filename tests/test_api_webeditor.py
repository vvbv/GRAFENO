"""Tests of the web editor link API (POST /api/v1/editor/open, WS editor.open)."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest

from grafeno import config as config_module
from grafeno import models
from grafeno.config import Config, StartupAction
from grafeno.server import ws, webeditor
from grafeno.server.actions import ApiError


class _Service:
    app = None


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = (tmp_path / "code" / "Demo").resolve()
    root.mkdir(parents=True)
    models.save(models.Task.create("T", "d", str(root), Config()))
    return root


@pytest.fixture
def settings_file(tmp_path):
    return tmp_path / "s.json"


def _configure(settings_file):
    cfg = config_module.load()
    cfg.startup = [StartupAction(name="ed", command="ed", open_url="http://{host}:9000/?folder={folder}",
                                 settings_file=str(settings_file))]
    config_module.save(cfg)


def _raises(status, params):
    with pytest.raises(ApiError) as info:
        webeditor.open_editor(_Service(), params)
    assert info.value.status == status
    return info.value


def test_no_editor_configured(project):
    _raises(404, {"workdir": str(project)})


def test_open_by_workdir_enforces_settings(project, settings_file):
    _configure(settings_file)
    result = webeditor.open_editor(_Service(), {"workdir": str(project)})
    assert result["url"] == f"http://localhost:9000/?folder={quote(str(project), safe='/:')}"
    assert result["running"] is None and result["settings_updated"] is True
    assert json.loads(settings_file.read_text())["git.repositoryScanMaxDepth"] == -1
    assert webeditor.open_editor(_Service(), {"workdir": str(project)})["settings_updated"] is False


def test_open_by_task_with_host(project, settings_file):
    _configure(settings_file)
    task = models.list_all()[0]
    result = webeditor.open_editor(_Service(), {"task_id": task.id, "host": "192.168.1.2"})
    assert result["url"].startswith("http://192.168.1.2:9000/")


def test_errors(project, settings_file):
    _configure(settings_file)
    _raises(404, {"task_id": "nope"})
    _raises(400, {})
    _raises(400, {"workdir": str(project), "host": "a b"})


def test_remote_task_rejected(project, settings_file):
    _configure(settings_file)
    task = models.list_all()[0]
    task.remote = "u@h:/p"
    models.save(task)
    _raises(409, {"task_id": task.id})


def test_unreadable_settings_file_is_reported_not_fatal(project, settings_file):
    _configure(settings_file)
    settings_file.write_text("not json", encoding="utf-8")
    result = webeditor.open_editor(_Service(), {"workdir": str(project)})
    assert result["settings_error"]
    assert settings_file.read_text(encoding="utf-8") == "not json"


def test_ws_method(project, settings_file):
    _configure(settings_file)
    result = asyncio.run(ws.METHODS["editor.open"](_Service(), {"workdir": str(project)}, None))
    assert result["url"].startswith("http://localhost:9000/")
