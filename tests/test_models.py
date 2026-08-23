"""Tests del modelo de tarea."""

from __future__ import annotations

import tomllib

from grafeno import _toml, models, paths
from grafeno.config import Config
from grafeno.models import Task, TaskState


def test_slugify():
    assert models.slugify("Añadir endpoint /health!") == "anadir-endpoint-health"
    assert models.slugify("   ") == "tarea"
    assert len(models.slugify("x" * 200)) <= 40


def test_task_create_snapshots_config(tmp_path):
    cfg = Config()
    cfg.planner.model = "p-model"
    cfg.final.model = "f-model"
    cfg.automode.enabled = True
    cfg.automode.test_command = "make test"
    cfg.automode.confirm_plan = True
    cfg.final_prompt = "instrucciones globales"
    task = Task.create("Demo", "desc", str(tmp_path), cfg)
    assert task.planner.model == "p-model"
    assert task.final.model == "f-model"
    assert task.automode is True
    assert task.test_command == "make test"
    assert task.confirm_plan is True
    assert task.final_prompt == "instrucciones globales"
    assert task.state is TaskState.DRAFT


def test_task_final_prompt_override(tmp_path):
    task = Task.create("Demo", "desc", str(tmp_path), Config(), final_prompt="override")
    assert task.final_prompt == "override"
    models.save(task)
    assert models.load(task.id).final_prompt == "override"


def test_task_confirm_plan_override(tmp_path):
    task = Task.create("Demo", "desc", str(tmp_path), Config(), confirm_plan=True)
    assert task.confirm_plan is True
    models.save(task)
    assert models.load(task.id).confirm_plan is True


def test_task_create_branch_override(tmp_path):
    task = Task.create("Demo", "desc", str(tmp_path), Config(), create_branch=False)
    assert task.create_branch is False
    models.save(task)
    assert models.load(task.id).create_branch is False


def test_task_create_branch_default_from_config(tmp_path):
    cfg = Config()
    cfg.automode.create_branch = False
    task = Task.create("Demo", "desc", str(tmp_path), cfg)
    assert task.create_branch is False


def test_cycles_roundtrip(tmp_path):
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    assert task.cycle == 1
    assert task.current_extension == ""
    task.start_new_cycle("primera ampliación\ncon detalle")
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.cycle == 2
    assert loaded.current_extension == "primera ampliación\ncon detalle"
    assert loaded.state is TaskState.DRAFT
    assert loaded.iteration == 0


def test_task_roundtrip(tmp_path):
    task = Task.create("Demo ñ", "desc\nmultilínea", str(tmp_path), Config())
    task.sessions["implementer"] = "ses_123"
    task.iteration = 2
    task.branch = "grafeno/demo-n"
    task.final_prompt = "instrucciones\nmultilínea"
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.id == task.id
    assert loaded.name == "Demo ñ"
    assert loaded.description == "desc\nmultilínea"
    assert loaded.sessions == {"implementer": "ses_123"}
    assert loaded.iteration == 2
    assert loaded.branch == "grafeno/demo-n"
    assert loaded.final_prompt == "instrucciones\nmultilínea"

    listed = models.list_all()
    assert [t.id for t in listed] == [task.id]


def test_task_final_role_roundtrip_and_legacy(tmp_path):
    """El rol final persiste; las tareas antiguas sin sección [final] cargan por defecto."""
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    task.final.cli = "kimi"
    task.final.model = "kimi-code/k3"
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.final.cli == "kimi"
    assert loaded.final.model == "kimi-code/k3"

    # Simula una tarea antigua: task.toml sin sección [final].
    meta = paths.task_meta_path(task.id)
    with meta.open("rb") as handle:
        data = tomllib.load(handle)
    data.pop("final", None)
    meta.write_text(_toml.dumps(data), encoding="utf-8")
    legacy = models.load(task.id)
    assert legacy.final.cli == "opencode"  # default_cli de from_dict
    assert legacy.final.model == ""


def test_task_tokens_roundtrip(tmp_path):
    from grafeno.drivers.base import TokenUsage

    task = models.Task.create("Demo", "desc", str(tmp_path), Config())
    task.record_tokens("prov/Model-X", TokenUsage(input=100, output=40))
    task.record_tokens("prov/Model-X", TokenUsage(input=50, output=10))
    task.record_tokens("", TokenUsage(input=5, output=2))  # modelo por defecto
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.tokens == task.tokens
    assert loaded.token_totals() == (155, 52)
    assert loaded.tokens_by_model() == {
        "prov/Model-X": (150, 50),
        "default": (5, 2),
    }
