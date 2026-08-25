"""Tests of the completion hooks engine."""

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
    assert hooks.parse_stages("plan,inventada") == ["plan"]  # unknown stages are ignored
    assert hooks.format_stages(["review", "plan"]) == "plan,review"


def test_resolve_global_only(tmp_path):
    _save_global(command="echo g", stages="plan,final")
    task = _make_task(tmp_path)
    assert hooks.resolve_commands(task, "plan") == ["echo g"]
    assert hooks.resolve_commands(task, "review") == []


def test_resolve_task_override(tmp_path):
    _save_global(command="echo g", stages="plan")
    task = _make_task(tmp_path, hook_command="echo t", hook_stages="plan")
    assert hooks.resolve_commands(task, "plan") == ["echo t"]  # override by default


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
    ))  # does not raise even if the hook fails
    assert any("{code}" not in m and "3" in m for m in infos)


def test_is_url():
    assert hooks.is_url("https://api.telegram.org/bot1/sendMessage")
    assert hooks.is_url("http://localhost:8080/hook")
    assert not hooks.is_url("echo hola")
    assert not hooks.is_url("./notify.sh --flag")


def test_build_webhook_url_placeholder():
    url = "https://h.test/send?chat_id=1&text={message}&x=1"
    built = hooks.build_webhook_url(url, "hola mundo")
    assert built == "https://h.test/send?chat_id=1&text=hola%20mundo&x=1"


def test_build_webhook_url_default_text_param():
    url = "https://api.telegram.org/botT/sendMessage?chat_id=-100&text=PENE"
    built = hooks.build_webhook_url(url, "tarea lista")
    assert "chat_id=-100" in built
    assert "PENE" not in built
    assert "text=tarea+lista" in built or "text=tarea%20lista" in built


def test_build_message_contains_context(tmp_path):
    task = _make_task(tmp_path)
    message = hooks.build_message(task, "plan", "ok")
    assert task.name in message
    assert "ok" in message


def test_run_stage_hooks_webhook_sends_message(tmp_path, monkeypatch):
    sent: list[str] = []

    async def fake_send(url: str) -> int:
        sent.append(url)
        return 200

    monkeypatch.setattr(hooks, "_send_webhook", fake_send)
    _save_global()
    task = _make_task(
        tmp_path,
        hook_command="https://h.test/sendMessage?chat_id=1&text={message}",
        hook_stages="plan",
    )
    infos: list[str] = []
    _run(hooks.run_stage_hooks(
        task, "plan", "ok",
        on_event=lambda phase, event: None,
        on_info=infos.append,
    ))
    assert len(sent) == 1
    assert "chat_id=1" in sent[0]
    assert "{message}" not in sent[0]
    assert "?" in sent[0] and "text=" in sent[0]


def test_run_stage_hooks_webhook_never_raises(tmp_path, monkeypatch):
    async def failing_send(url: str) -> int:
        raise OSError("sin red")

    monkeypatch.setattr(hooks, "_send_webhook", failing_send)
    _save_global(command="https://h.test/x?text={message}", stages="plan")
    task = _make_task(tmp_path)
    infos: list[str] = []
    _run(hooks.run_stage_hooks(
        task, "plan", "ok",
        on_event=lambda phase, event: None,
        on_info=infos.append,
    ))  # does not raise even if the webhook fails
    assert any("sin red" in m for m in infos)


def test_run_stage_hooks_webhook_does_not_log_query(tmp_path, monkeypatch):
    async def fake_send(url: str) -> int:
        return 200

    monkeypatch.setattr(hooks, "_send_webhook", fake_send)
    _save_global(command="https://h.test/x?token=SECRETO&text={message}", stages="plan")
    task = _make_task(tmp_path)
    infos: list[str] = []
    _run(hooks.run_stage_hooks(
        task, "plan", "ok",
        on_event=lambda phase, event: None,
        on_info=infos.append,
    ))
    assert all("SECRETO" not in m for m in infos)
