"""Tests of the prompt templates and their contracts."""

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
    assert "OPTIMIZA el plan" in prompt  # optimisation for the executor


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
    # The cycle 2 review goes to its own directory.
    assert "ciclo-02" in prompts.review_prompt(task, 1)


def test_code_rules_in_prompts(tmp_path):
    """Senior role, no emotes and English documentation (or project style)."""
    task = _task(tmp_path)
    plan = prompts.plan_prompt(task)
    assert "INGENIERO DE SOFTWARE SENIOR" in plan
    assert "emotes" in plan
    assert "INGLÉS" in plan
    assert "estilo de codificación" in plan
    # The rules also reach whoever writes the code.
    assert "emotes" in prompts.implement_prompt(task)
    assert "INGLÉS" in prompts.fix_prompt(task, 1)


def test_suggestions_for_complex_methods(tmp_path):
    """The planner suggests on complex methods and the reviewer does too."""
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
    """The final steps prompt points to the report, requires rules and mentions tests."""
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
    """If the task defines final_prompt, the prompt includes it as a dedicated section."""
    task = _task(tmp_path, final_prompt="Revisa el CHANGELOG")
    prompt = prompts.final_prompt(task)
    assert "# Instrucciones adicionales del usuario para el cierre" in prompt
    assert "Revisa el CHANGELOG" in prompt


def test_final_prompt_without_custom_instructions_unchanged(tmp_path):
    """Without final_prompt (or only whitespace), the prompt does not add the extra section."""
    task_empty = _task(tmp_path)
    prompt_empty = prompts.final_prompt(task_empty)
    assert "Instrucciones adicionales" not in prompt_empty

    task_blank = _task(tmp_path, final_prompt="   \n  ")
    prompt_blank = prompts.final_prompt(task_blank)
    assert "Instrucciones adicionales" not in prompt_blank
    # The rest of the contract remains intact.
    assert "AGENTE DE PASOS FINALES" in prompt_blank


def test_common_rules_include_git_author_rule(tmp_path):
    """All prompts require respecting the git author configured in the system."""
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
        assert "mensajes de commit" in prompt
        assert "INGLÉS" in prompt
        assert "AGENTS.md" in prompt


def test_reevaluate_plan_prompt_includes_task_context(tmp_path):
    """The re-evaluation prompt includes name, description and plan path."""
    task = _task(tmp_path)
    task.description = "una descripción concreta y única para el test"
    prompt = prompts.reevaluate_plan_prompt(task)
    assert task.name in prompt
    assert task.description in prompt
    assert str(paths.plan_dir(task.id)) in prompt
    assert "GRAFENO-EXECUTOR" in prompt
    assert "REEVALUACIÓN" in prompt


def test_prompts_without_references_omit_section(tmp_path):
    """With no resolved references, neither prompt adds the section."""
    task = _task(tmp_path)
    plan = prompts.plan_prompt(task)
    reevaluate = prompts.reevaluate_plan_prompt(task)
    implement = prompts.implement_prompt(task)
    assert "Referencias de contexto" not in plan
    assert "Referencias de contexto" not in reevaluate
    assert "Referencias de contexto" not in implement


def test_prompts_inject_task_references(tmp_path):
    """Resolved references appear in plan, reevaluate and implement."""
    from grafeno.references import Reference

    task = _task(tmp_path)
    task.references = [
        Reference(
            name="GUI ref",
            path="https://example.com",
            description="UI inspiration",
        ),
    ]
    plan = prompts.plan_prompt(task)
    reevaluate = prompts.reevaluate_plan_prompt(task)
    implement = prompts.implement_prompt(task)
    review = prompts.review_prompt(task, 1)
    for prompt in (plan, reevaluate, implement):
        assert "Referencias de contexto" in prompt
        assert "GUI ref" in prompt
        assert "https://example.com" in prompt
        assert "UI inspiration" in prompt
        assert "tokens" in prompt  # the warning is present
    # Review, fix and final are intentionally without the section.
    assert "Referencias de contexto" not in review
    assert "Referencias de contexto" not in prompts.fix_prompt(task, 1)
    assert "Referencias de contexto" not in prompts.final_prompt(task)


def test_global_references_excluded_when_flag_false(tmp_path):
    """``use_global_references=False`` keeps global refs out of the prompt."""
    from grafeno import references as references_module
    from grafeno.references import Reference

    references_module.save_global([Reference(name="global-ref", path="/g")])
    task = _task(tmp_path)
    task.use_global_references = False
    plan = prompts.plan_prompt(task)
    assert "global-ref" not in plan


def test_remote_section_in_all_phase_prompts(tmp_path):
    task = _task(tmp_path, remote="u@h:/srv/app", remote_os="Linux 6.1 x86_64")
    prompts_to_check = [
        prompts.plan_prompt(task),
        prompts.reevaluate_plan_prompt(task),
        prompts.implement_prompt(task),
        prompts.review_prompt(task, 1),
        prompts.fix_prompt(task, 1),
        prompts.final_prompt(task),
    ]
    for prompt in prompts_to_check:
        assert "Entorno remoto (SSH)" in prompt
        assert "Linux 6.1 x86_64" in prompt


def test_remote_section_mandate_only_in_plan_prompts(tmp_path):
    task = _task(tmp_path, remote="u@h:/srv/app", remote_os="Linux 6.1 x86_64")
    assert 'sección "Entorno remoto"' in prompts.plan_prompt(task)
    assert 'sección "Entorno remoto"' in prompts.reevaluate_plan_prompt(task)
    assert 'sección "Entorno remoto"' not in prompts.implement_prompt(task)


def test_remote_section_absent_for_local_tasks(tmp_path):
    task = _task(tmp_path)
    assert "Entorno remoto" not in prompts.plan_prompt(task)
    assert "Entorno remoto" not in prompts.implement_prompt(task)


def test_remote_section_fallback_when_os_unknown(tmp_path):
    task = _task(tmp_path, remote="u@h:/srv/app")
    assert "no detectado" in prompts.plan_prompt(task)


def test_remote_section_session(tmp_path):
    """In session mode the section is injected even without task.remote."""
    from grafeno import remote, remotesession

    remote.set_session(
        remote.RemoteSpec(user="root", host="h", path="/root"),
        mounts_base=tmp_path,
    )
    remotesession._current = remotesession.RemoteSession(
        spec=remote.RemoteSpec(user="root", host="h", path="/root"),
        remote_home="/root",
        remote_os="Linux x86_64",
    )
    try:
        task = _task(tmp_path, workdir="/srv/app")
        prompt = prompts.plan_prompt(task)
        assert "Entorno remoto (SSH)" in prompt
        assert "root@h" in prompt
        assert "Linux x86_64" in prompt
    finally:
        remotesession.deactivate()


def test_prompts_without_media_omit_section(tmp_path):
    """Without images, no media section is appended to the prompts."""
    task = _task(tmp_path)
    for prompt in (
        prompts.plan_prompt(task),
        prompts.reevaluate_plan_prompt(task),
        prompts.implement_prompt(task),
        prompts.review_prompt(task, 1),
        prompts.fix_prompt(task, 1),
        prompts.final_prompt(task),
    ):
        assert "Imágenes adjuntas" not in prompt


def test_prompts_inject_media_paths(tmp_path):
    """Images are listed in plan/reevaluate/implement; not in review/fix/final."""
    task = _task(tmp_path)
    # Fake image: media.list_media only lists the PNG, it does not read it,
    # so the bytes do not need to be a real PNG.
    media_dir = paths.media_dir(task.id)
    (media_dir / "media-01.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    absolute = str((media_dir / "media-01.png").resolve())
    plan = prompts.plan_prompt(task)
    reevaluate = prompts.reevaluate_plan_prompt(task)
    implement = prompts.implement_prompt(task)
    review = prompts.review_prompt(task, 1)
    fix = prompts.fix_prompt(task, 1)
    final = prompts.final_prompt(task)
    for prompt in (plan, reevaluate, implement):
        assert "Imágenes adjuntas" in prompt
        assert absolute in prompt
    for prompt in (review, fix, final):
        assert "Imágenes adjuntas" not in prompt
        assert absolute not in prompt
