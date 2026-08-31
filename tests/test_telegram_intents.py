"""Tests of the Telegram intent parser (fake CLI drivers)."""

from __future__ import annotations

import asyncio
import json
from collections import deque

from grafeno import models
from grafeno.config import Config
from grafeno.drivers.base import CLIDriver, RunResult
from grafeno.telegram import intents
from grafeno.telegram.intents import Intent, TaskSpec


class FakeDriver(CLIDriver):
    """Driver double: returns queued RunResults, records prompts."""

    def __init__(self, results: list[RunResult], available: bool = True):
        self.name = "fake"
        self.display_name = "fake"
        self.executable = "fake"
        self._available = available
        self._results = deque(results)
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def models_command(self):
        return []

    def parse_models(self, output):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.prompts.append(request.prompt)
        if self._results:
            return self._results.popleft()
        return RunResult(ok=True, text="ok")


def _ok(text: str) -> RunResult:
    return RunResult(ok=True, text=text)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------- #
# TaskSpec / parse_intent_payload
# ---------------------------------------------------------------------- #
def test_taskspec_roundtrip():
    spec = TaskSpec(name="n", description="d", workdir="/w", test_command="pytest")
    assert TaskSpec.from_dict(spec.to_dict()) == spec


def test_parse_payload_create_multiple_tasks():
    payload = json.dumps({
        "action": "create_tasks",
        "tasks": [
            {"name": "t1", "description": "d1"},
            {"name": "t2", "description": "d2", "workdir":"/w", "test_command": "make test"},
        ],
    })
    intent = intents.parse_intent_payload(payload)
    assert intent.action == "create_tasks"
    assert [s.name for s in intent.tasks] == ["t1", "t2"]
    assert intent.tasks[1].workdir == "/w"
    assert intent.tasks[1].test_command == "make test"


def test_parse_payload_extracts_json_from_prose():
    text = 'Claro, aquí va:\n```json\n{"action": "list_tasks"}\n```\nListo.'
    intent = intents.parse_intent_payload(text)
    assert intent.action == "list_tasks"


def test_parse_payload_without_json_is_unknown():
    assert intents.parse_intent_payload("no json at all").action == "unknown"
    assert intents.parse_intent_payload("{broken json").action == "unknown"


def test_parse_payload_unknown_action_normalized():
    assert intents.parse_intent_payload('{"action": "borra_todo"}').action == "unknown"


def test_parse_payload_create_without_valid_tasks_is_unknown():
    payload = '{"action": "create_tasks", "tasks": [{"description": "sin nombre"}]}'
    assert intents.parse_intent_payload(payload).action == "unknown"


def test_parse_payload_query_without_ref_becomes_help():
    payload = '{"action": "task_status"}'
    assert intents.parse_intent_payload(payload).action == "help"


def test_parse_payload_ask_keeps_question_and_ref():
    payload = json.dumps({
        "action": "ask", "task_ref": "grafeno", "question": "¿por qué falló?",
    })
    intent = intents.parse_intent_payload(payload)
    assert intent.action == "ask"
    assert intent.task_ref == "grafeno"
    assert intent.question == "¿por qué falló?"


def test_parse_payload_tasks_capped():
    payload = json.dumps({
        "action": "create_tasks",
        "tasks": [{"name": f"t{n}"} for n in range(30)],
    })
    intent = intents.parse_intent_payload(payload)
    assert len(intent.tasks) == intents.MAX_TASKS_PER_INTENT


# ---------------------------------------------------------------------- #
# parse_intent (CLI one-shot)
# ---------------------------------------------------------------------- #
def test_parse_intent_ok(tmp_path):
    driver = FakeDriver([_ok('{"action": "list_tasks"}')])
    intent = _run(intents.parse_intent(driver, "modelo", "lista", "- t1 | a | b", tmp_path))
    assert intent.action == "list_tasks"
    # The prompt carries the user text and the task summary.
    assert "lista" in driver.prompts[0]
    assert "- t1 | a | b" in driver.prompts[0]


def test_parse_intent_cli_failure_carries_error(tmp_path):
    """A failing CLI surfaces the error (distinct from a plain 'unknown')."""
    driver = FakeDriver([RunResult(ok=False, error="boom exit 1")])
    intent = _run(intents.parse_intent(driver, "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert "boom" in intent.error


def test_parse_intent_cli_exception_carries_error(tmp_path):
    class BoomDriver(FakeDriver):
        async def run(self, request, on_event=None, on_activity=None):
            raise RuntimeError("crash")

    intent = _run(intents.parse_intent(BoomDriver([]), "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert "crash" in intent.error


def test_parse_intent_timeout_carries_error(tmp_path):
    """A hung parser CLI is cancelled after the timeout and reported."""
    class SlowDriver(FakeDriver):
        async def run(self, request, on_event=None, on_activity=None):
            await asyncio.sleep(30)
            return RunResult(ok=True, text="{}")

    intent = _run(intents.parse_intent(SlowDriver([]), "", "texto", "", tmp_path, timeout=0.2))
    assert intent.action == "unknown"
    assert "timeout" in intent.error


def test_parse_intent_bad_json_is_unknown_without_error(tmp_path):
    driver = FakeDriver([_ok("no json here")])
    intent = _run(intents.parse_intent(driver, "", "texto", "", tmp_path))
    assert intent.action == "unknown"
    assert intent.error == ""


# ---------------------------------------------------------------------- #
# fuzzy_find_task / tasks_summary
# ---------------------------------------------------------------------- #
def _task(name: str) -> object:
    return models.Task.create(name, "desc", ".", Config())


def test_fuzzy_find_by_id_prefix_and_name():
    tasks = [_task("Arreglar login"), _task("Mejorar logs")]
    assert intents.fuzzy_find_task(tasks[0].id, tasks) is tasks[0]
    prefix = tasks[1].id[:18]
    assert intents.fuzzy_find_task(prefix, tasks) is tasks[1]
    assert intents.fuzzy_find_task("arreglar login", tasks) is tasks[0]
    assert intents.fuzzy_find_task("logs", tasks) is tasks[1]
    assert intents.fuzzy_find_task("inexistente", tasks) is None
    assert intents.fuzzy_find_task("", tasks) is None


def test_tasks_summary_format():
    tasks = [_task("Demo")]
    summary = intents.tasks_summary(tasks)
    assert tasks[0].id in summary
    assert "Demo" in summary
