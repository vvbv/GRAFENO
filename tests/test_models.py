"""Tests del modelo de tarea."""

from __future__ import annotations

import tomllib

from grafeno import _toml, models, paths
from grafeno.config import Config
from grafeno.models import Task, TaskState, reset_to_draft, state_label


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


def test_task_hook_fields_roundtrip(tmp_path):
    task = models.Task.create(
        "Demo", "d", str(tmp_path), Config(),
        hook_command="./notify.sh", hook_stages="review,fix", hook_mode="both",
    )
    models.save(task)
    loaded = models.load(task.id)
    assert loaded.hook_command == "./notify.sh"
    assert loaded.hook_stages == "review,fix"
    assert loaded.hook_mode == "both"


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
    task.record_tokens("opencode", "prov/Model-X", "implement", TokenUsage(input=100, output=40))
    task.record_tokens("opencode", "prov/Model-X", "review", TokenUsage(input=50, output=10))
    task.record_tokens("kimi", "", "plan", TokenUsage(input=5, output=2))
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.tokens == task.tokens
    assert loaded.token_totals() == (155, 52)
    assert loaded.tokens_by_cli_model() == {
        "opencode/prov/Model-X": (150, 50),
        "kimi/default": (5, 2),
    }
    assert loaded.tokens_by_phase() == {
        "implement": (100, 40),
        "review": (50, 10),
        "plan": (5, 2),
    }


def test_legacy_token_keys_aggregated_as_legacy_phase(tmp_path):
    """Las claves antiguas "{modelo}|{input|output}" se agregan como fase legacy."""
    task = models.Task.create("Demo", "desc", str(tmp_path), Config())
    models.save(task)
    meta = paths.task_meta_path(task.id)
    with meta.open("rb") as handle:
        data = tomllib.load(handle)
    data["tokens"] = {"prov/Model-X|input": 100, "prov/Model-X|output": 40}
    meta.write_text(_toml.dumps(data), encoding="utf-8")

    loaded = models.load(task.id)
    assert loaded.token_totals() == (100, 40)
    assert loaded.tokens_by_phase() == {models.LEGACY_PHASE: (100, 40)}
    assert loaded.tokens_by_cli_model() == {"prov/Model-X": (100, 40)}


def test_cli_model_label():
    """La etiqueta de agente es 'cli/modelo' o solo el modelo sin cli."""
    assert models.cli_model_label("opencode", "prov/x") == "opencode/prov/x"
    assert models.cli_model_label("", "default") == "default"


def test_discarded_state_roundtrip():
    """El estado DISCARDED se persiste y se lee correctamente."""
    task = Task.create("Descartable", "desc", "/tmp", Config())
    task.state = TaskState.DISCARDED
    models.save(task)
    loaded = models.load(task.id)
    assert loaded.state is TaskState.DISCARDED
    assert state_label(loaded.state) == "Discarded"


def test_reset_to_draft_limpia_estado_y_artefactos(tmp_path):
    """reset_to_draft devuelve la tarea a DRAFT y borra plan/review/final."""
    task = Task.create("Reinicio", "desc", str(tmp_path), Config())
    models.save(task)
    task.state = TaskState.IMPLEMENTED
    task.iteration = 3
    task.cycle = 2
    task.sessions = {"planner": "s1"}
    task.extensions = {"2": "mas cosas"}
    task.scheduled_at = "2030-01-01T10:00"
    models.save(task)
    # Artefactos del ciclo 1 y de una ampliación.
    (paths.plan_dir(task.id) / "01-plan.md").write_text("plan", encoding="utf-8")
    (paths.review_dir(task.id) / "01-review.md").write_text("rev", encoding="utf-8")
    (paths.final_dir(task.id) / "01-final.md").write_text("fin", encoding="utf-8")
    ciclo2 = paths.plan_dir(task.id, 2)
    (ciclo2 / "01-plan.md").write_text("plan2", encoding="utf-8")

    reset_to_draft(task)

    persisted = models.load(task.id)
    assert persisted.state is TaskState.DRAFT
    assert persisted.iteration == 0
    assert persisted.cycle == 1
    assert persisted.sessions == {}
    assert persisted.extensions == {}
    assert persisted.scheduled_at == ""
    # Los artefactos desaparecen; los directorios quedan recreados vacíos.
    assert list(paths.plan_dir(task.id).glob("**/*.md")) == []
    assert list(paths.review_dir(task.id).glob("**/*.md")) == []
    assert list(paths.final_dir(task.id).glob("**/*.md")) == []
    # Logs y metadatos sobreviven.
    assert paths.logs_dir(task.id).is_dir()
    assert paths.task_meta_path(task.id).is_file()


def test_reset_to_draft_conserva_tokens_y_rama(tmp_path):
    """El reinicio no toca tokens, duraciones ni la rama git."""
    task = Task.create("Conservar", "desc", str(tmp_path), Config())
    models.save(task)
    task.branch = "grafeno/conservar"
    task.durations = {"plan": 12}
    task.tokens = {"plan|opencode|m|input": 100}
    models.save(task)

    reset_to_draft(task)

    persisted = models.load(task.id)
    assert persisted.branch == "grafeno/conservar"
    assert persisted.durations == {"plan": 12}
    assert persisted.tokens == {"plan|opencode|m|input": 100}


def test_total_duration_seconds(tmp_path):
    """total_duration_seconds sums every phase; empty means zero."""
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    assert task.total_duration_seconds() == 0
    task.durations = {"plan": 12, "implement": 30, "tests": 5}
    assert task.total_duration_seconds() == 47


def test_task_create_copies_effort_from_config(tmp_path):
    """Task.create propaga ``effort`` desde cada ``RoleConfig`` global."""
    cfg = Config()
    cfg.planner.effort = "low"
    cfg.implementer.effort = "max"
    cfg.reviewer.effort = "medium"
    cfg.final.effort = "high"
    task = Task.create("Demo", "desc", str(tmp_path), cfg)
    assert task.planner.effort == "low"
    assert task.implementer.effort == "max"
    assert task.reviewer.effort == "medium"
    assert task.final.effort == "high"


def test_task_effort_roundtrip(tmp_path):
    """El esfuerzo de cada rol persiste en task.toml y se recupera."""
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    task.planner.effort = "low"
    task.implementer.effort = "max"
    task.reviewer.effort = "medium"
    task.final.effort = "high"
    models.save(task)

    loaded = models.load(task.id)
    assert loaded.planner.effort == "low"
    assert loaded.implementer.effort == "max"
    assert loaded.reviewer.effort == "medium"
    assert loaded.final.effort == "high"


def test_legacy_task_without_effort_loads_empty(tmp_path):
    """Tareas antiguas sin clave ``effort`` cargan con valor vacío."""
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    models.save(task)
    meta = paths.task_meta_path(task.id)
    with meta.open("rb") as handle:
        data = tomllib.load(handle)
    for role_key in ("planner", "implementer", "reviewer", "final"):
        data[role_key].pop("effort", None)
    meta.write_text(_toml.dumps(data), encoding="utf-8")

    loaded = models.load(task.id)
    assert loaded.planner.effort == ""
    assert loaded.implementer.effort == ""
    assert loaded.reviewer.effort == ""
    assert loaded.final.effort == ""
