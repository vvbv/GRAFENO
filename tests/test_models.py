"""Tests del modelo de tarea."""

from __future__ import annotations

from grafeno import models
from grafeno.config import Config
from grafeno.models import Task, TaskState


def test_slugify():
    assert models.slugify("Añadir endpoint /health!") == "anadir-endpoint-health"
    assert models.slugify("   ") == "tarea"
    assert len(models.slugify("x" * 200)) <= 40


def test_task_create_snapshots_config(tmp_path):
    cfg = Config()
    cfg.planner.model = "p-model"
    cfg.automode.enabled = True
    cfg.automode.test_command = "make test"
    task = Task.create("Demo", "desc", str(tmp_path), cfg)
    assert task.planner.model == "p-model"
    assert task.automode is True
    assert task.test_command == "make test"
    assert task.state is TaskState.DRAFT


def test_task_roundtrip(tmp_path):
    task = Task.create("Demo ñ", "desc\nmultilínea", str(tmp_path), Config())
    task.sessions["implementer"] = "ses_123"
    task.iteration = 2
    task.branch = "grafeno/demo-n"
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.id == task.id
    assert loaded.name == "Demo ñ"
    assert loaded.description == "desc\nmultilínea"
    assert loaded.sessions == {"implementer": "ses_123"}
    assert loaded.iteration == 2
    assert loaded.branch == "grafeno/demo-n"

    listed = models.list_all()
    assert [t.id for t in listed] == [task.id]
