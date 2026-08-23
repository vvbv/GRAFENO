"""Tests de las plantillas de prompts y sus contratos."""

from __future__ import annotations

from grafeno import paths
from grafeno.config import Config
from grafeno.models import Task
from grafeno.pipeline import prompts


def _task(tmp_path, **overrides) -> Task:
    cfg = Config()
    cfg.implementer.cli = "kimi"
    cfg.implementer.model = "kimi-code/k3"
    task = Task.create("Demo", "hacer algo", str(tmp_path), cfg)
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def test_plan_prompt_includes_executor_contract(tmp_path):
    task = _task(tmp_path)
    prompt = prompts.plan_prompt(task)
    assert "GRAFENO-EXECUTOR" in prompt
    assert "cli: kimi" in prompt
    assert "model: kimi-code/k3" in prompt
    assert str(paths.plan_dir(task.id)) in prompt
    assert "NN-slug.md" in prompt
    assert "OPTIMIZA el plan" in prompt  # optimización para el ejecutor


def test_plan_prompt_mentions_tests_when_defined(tmp_path):
    task = _task(tmp_path, test_command="pytest -q")
    prompt = prompts.plan_prompt(task)
    assert "pytest -q" in prompt


def test_implement_prompt_points_to_plan_dir(tmp_path):
    task = _task(tmp_path)
    prompt = prompts.implement_prompt(task)
    assert str(paths.plan_dir(task.id)) in prompt
    assert task.workdir in prompt


def test_review_prompt_requires_verdict_and_file(tmp_path):
    task = _task(tmp_path)
    prompt = prompts.review_prompt(task, 2)
    assert "VERDICT: APPROVED" in prompt
    assert "VERDICT: CHANGES_REQUESTED" in prompt
    assert "02-review.md" in prompt


def test_fix_prompt_references_review_file(tmp_path):
    task = _task(tmp_path)
    prompt = prompts.fix_prompt(task, 3)
    assert "03-review.md" in prompt
    assert str(paths.plan_dir(task.id)) in prompt


def test_plan_prompt_cycle_includes_extension(tmp_path):
    task = _task(tmp_path)
    task.start_new_cycle("añade caché también a los precios")
    prompt = prompts.plan_prompt(task)
    assert "Ampliación" in prompt
    assert "añade caché también a los precios" in prompt
    assert str(paths.plan_dir(task.id, 2)) in prompt
    # La revisión del ciclo 2 va a su propio directorio.
    assert "ciclo-02" in prompts.review_prompt(task, 1)


def test_code_rules_in_prompts(tmp_path):
    """Rol senior, sin emotes y documentación en inglés (o estilo del proyecto)."""
    task = _task(tmp_path)
    plan = prompts.plan_prompt(task)
    assert "INGENIERO DE SOFTWARE SENIOR" in plan
    assert "emotes" in plan
    assert "INGLÉS" in plan
    assert "estilo de codificación" in plan
    # Las reglas llegan también a quien escribe el código.
    assert "emotes" in prompts.implement_prompt(task)
    assert "INGLÉS" in prompts.fix_prompt(task, 1)


def test_suggestions_for_complex_methods(tmp_path):
    """El planificador sugiere ante métodos complejos y el revisor también."""
    task = _task(tmp_path)
    plan = prompts.plan_prompt(task)
    assert "COMPLEJO" in plan
    assert "Sugerencias" in plan
    assert "descomposición" in plan
    assert "pseudocódigo" in plan

    review = prompts.review_prompt(task, 1)
    assert "SUGERENCIAS" in review
    assert "complejos" in review
    assert "Sugerencias de mejora" in review


def test_final_prompt_contract(tmp_path):
    """El prompt de pasos finales apunta al informe, exige reglas y menciona tests."""
    task = _task(tmp_path, test_command="pytest -q")
    prompt = prompts.final_prompt(task)
    assert str(paths.final_dir(task.id)) in prompt
    assert "01-final.md" in prompt
    assert task.workdir in prompt
    assert "pytest -q" in prompt
    assert "emotes" in prompt
    assert "INGLÉS" in prompt
    assert "AGENTE DE PASOS FINALES" in prompt


def test_final_prompt_custom_instructions(tmp_path):
    """Si la tarea define final_prompt, el prompt lo incluye como sección dedicada."""
    task = _task(tmp_path, final_prompt="Revisa el CHANGELOG")
    prompt = prompts.final_prompt(task)
    assert "# Instrucciones adicionales del usuario para el cierre" in prompt
    assert "Revisa el CHANGELOG" in prompt


def test_final_prompt_without_custom_instructions_unchanged(tmp_path):
    """Sin final_prompt (o solo espacios), el prompt no añade la sección extra."""
    task_empty = _task(tmp_path)
    prompt_empty = prompts.final_prompt(task_empty)
    assert "Instrucciones adicionales" not in prompt_empty

    task_blank = _task(tmp_path, final_prompt="   \n  ")
    prompt_blank = prompts.final_prompt(task_blank)
    assert "Instrucciones adicionales" not in prompt_blank
    # El resto del contrato sigue intacto.
    assert "AGENTE DE PASOS FINALES" in prompt_blank


def test_common_rules_include_git_author_rule(tmp_path):
    """Todos los prompts exigen respetar el autor git configurado en el sistema."""
    task = _task(tmp_path)
    for prompt in (
        prompts.plan_prompt(task),
        prompts.implement_prompt(task),
        prompts.review_prompt(task, 1),
        prompts.fix_prompt(task, 1),
        prompts.final_prompt(task),
    ):
        assert "git config user.name" in prompt
        assert "bajo ningún concepto" in prompt
