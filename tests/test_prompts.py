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
