"""Tests for grafeno.live_log."""

from __future__ import annotations

from rich.text import Text

from grafeno import live_log, paths


def test_roundtrip_preserves_text_and_style():
    live_log.append("t1", Text("plain line"))
    live_log.append("t1", Text("styled", style="bold red"))
    entries = live_log.load("t1", 100)
    assert [e.plain for e in entries] == ["plain line", "styled"]
    assert str(entries[1].style) == "bold red"


def test_load_missing_file_returns_empty():
    assert live_log.load("no-such-task", 100) == []


def test_load_respects_max_entries():
    for index in range(10):
        live_log.append("t2", Text(f"line {index}"))
    entries = live_log.load("t2", 3)
    assert [e.plain for e in entries] == ["line 7", "line 8", "line 9"]


def test_load_skips_corrupt_lines():
    log_path = paths.logs_dir("t3") / "live.jsonl"
    log_path.write_text(
        'not json\n{"style": "", "text": "ok"}\n', encoding="utf-8"
    )
    entries = live_log.load("t3", 100)
    assert [e.plain for e in entries] == ["ok"]


def test_clear_is_idempotent():
    live_log.append("t4", Text("x"))
    live_log.clear("t4")
    live_log.clear("t4")  # second call must not raise
    assert live_log.load("t4", 100) == []


def test_unicode_roundtrip():
    live_log.append("t5", Text("acción áéíóú ñ"))
    assert live_log.load("t5", 10)[0].plain == "acción áéíóú ñ"


def test_task_runtime_restores_persisted_log(tmp_path):
    """TaskRuntime loads the persisted log when it is constructed."""
    from grafeno.config import Config
    from grafeno.models import Task
    from grafeno.tui.runtime import TaskRuntime

    task = Task.create("Runtime restaura", "desc", str(tmp_path), Config())
    live_log.append(task.id, Text("hola persistido"))
    live_log.append(task.id, Text("estilo persistido", style="bold cyan"))

    runtime = TaskRuntime(task)
    assert [entry.plain for entry in runtime.log] == [
        "hola persistido",
        "estilo persistido",
    ]
    assert str(runtime.log[1].style) == "bold cyan"
