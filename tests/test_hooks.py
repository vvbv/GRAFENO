"""Tests del motor de hooks de completado."""

from __future__ import annotations

import asyncio

from grafeno import config as config_module
from grafeno import models
from grafeno.config import Config
from grafeno.pipeline import hooks


def _make_task(tmp_path, **overrides):
    task = models.Task.create("Demo", "desc", str(tmp_path), Config())
    for key, value in overrides.items():
        setattr(task, key, value)
    models.save(task)
    return task


def _save_global(command: str = "", stages: str = "") -> None:
    cfg = Config()
    cfg.hook.command = command
    cfg.hook.stages = stages
    config_module.save(cfg)


def _run(coro):
    return asyncio.run(coro)


def test_parse_and_format_stages_roundtrip():
    assert hooks.parse_stages("final, plan,review") == ["plan", "review", "final"]
    assert hooks.parse_stages("") == []
    assert hooks.parse_stages("plan,inventada") == ["plan"]  # ignora desconocidas
    assert hooks.format_stages(["review", "plan"]) == "plan,review"


def test_resolve_global_only(tmp_path):
    _save_global(command="echo g", stages="plan,final")
    task = _make_task(tmp_path)
    assert hooks.resolve_commands(task, "plan") == ["echo g"]
    assert hooks.resolve_commands(task, "review") == []


def test_resolve_task_override(tmp_path):
    _save_global(command="echo g", stages="plan")
    task = _make_task(tmp_path, hook_command="echo t", hook_stages="plan")
    assert hooks.resolve_commands(task, "plan") == ["echo t"]  # override por defecto


def test_resolve_task_both_order(tmp_path):
    _save_global(command="echo g", stages="plan")
    task = _make_task(
        tmp_path, hook_command="echo t", hook_stages="plan", hook_mode="both"
    )
    assert hooks.resolve_commands(task, "plan") == ["echo g", "echo t"]


def test_run_stage_hooks_executes_and_passes_env(tmp_path):
    _save_global()
    marker = tmp_path / "marker.txt"
    task = _make_task(
        tmp_path,
        hook_command='echo "$GRAFENO_PHASE:$GRAFENO_OUTCOME:$GRAFENO_TASK_ID" >> marker.txt',
        hook_stages="plan",
    )
    infos: list[str] = []
    _run(hooks.run_stage_hooks(
        task, "plan", "ok",
        on_event=lambda phase, event: None,
        on_info=infos.append,
    ))
    assert marker.read_text(encoding="utf-8").strip() == f"plan:ok:{task.id}"


def test_run_stage_hooks_never_raises_on_failure(tmp_path):
    _save_global(command="exit 3", stages="plan")
    task = _make_task(tmp_path)
    infos: list[str] = []
    _run(hooks.run_stage_hooks(
        task, "plan", "ok",
        on_event=lambda phase, event: None,
        on_info=infos.append,
    ))  # no lanza aunque el hook falle
    assert any("{code}" not in m and "3" in m for m in infos)
