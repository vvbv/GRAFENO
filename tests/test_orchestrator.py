"""Tests del orquestador con drivers falsos inyectados."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

from grafeno import paths
from grafeno.config import Config
from grafeno.drivers.base import CLIDriver, RunResult
from grafeno.models import Task, TaskState
from grafeno.pipeline.orchestrator import Orchestrator, PhaseError


class FakeDriver(CLIDriver):
    """Driver de prueba: devuelve resultados en cola, sin subprocesos."""

    def __init__(self, name: str, results: list[RunResult], available: bool = True):
        self.name = name
        self.display_name = name
        self.executable = name
        self._available = available
        self._results = deque(results)
        self.prompts: list[str] = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def list_models(self):
        return []

    async def run(self, request, on_event=None):
        self.prompts.append(request.prompt)
        if self._results:
            return self._results.popleft()
        return RunResult(ok=True, text="ok")


def _ok(text: str) -> RunResult:
    return RunResult(ok=True, text=text)


def _make_task(tmp_path, **overrides) -> Task:
    task = Task.create("Demo", "desc", str(tmp_path), Config())
    task.planner.cli = "fake-planner"
    task.implementer.cli = "fake-impl"
    task.reviewer.cli = "fake-rev"
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _run(coro):
    return asyncio.run(coro)


def test_automode_happy_path(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("contenido del plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("implementado")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("Todo bien.\nVERDICT: APPROVED")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 0
    # El plan se materializó con la cabecera de ejecutor (fallback de salida).
    plans = list(paths.plan_dir(task.id).glob("*.md"))
    assert len(plans) == 1
    content = plans[0].read_text(encoding="utf-8")
    assert "GRAFENO-EXECUTOR" in content
    assert "cli: fake-impl" in content
    assert "contenido del plan" in content


def test_automode_fix_loop_until_approved(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2 corregida")]),
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("Faltan cosas.\nVERDICT: CHANGES_REQUESTED"), _ok("Ahora sí.\nVERDICT: APPROVED")],
        ),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 1
    # La corrección recibió el prompt de fix que referencia la revisión 01.
    assert "01-review.md" in drivers["fake-impl"].prompts[-1]


def test_automode_max_iterations_exhausted(tmp_path):
    task = _make_task(tmp_path, max_iterations=2)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", []),  # siempre ok
        "fake-rev": FakeDriver("fake-rev", []),    # sin veredicto -> CHANGES_REQUESTED
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.FAILED
    assert task.iteration == 2


def test_phase_failure_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [RunResult(ok=False, error="boom")]),
        "fake-impl": FakeDriver("fake-impl", []),
        "fake-rev": FakeDriver("fake-rev", []),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.FAILED


def test_missing_cli_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {"fake-planner": FakeDriver("fake-planner", [], available=False)}
    orch = Orchestrator(task, drivers=drivers)
    try:
        _run(orch.run_plan())
        raise AssertionError("debió lanzar PhaseError")
    except PhaseError as exc:
        assert "fake-planner" in str(exc)
    assert task.state is TaskState.FAILED


def test_unknown_cli_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    task.planner.cli = "codex"  # previsto pero sin driver todavía
    orch = Orchestrator(task, drivers={})
    try:
        _run(orch.run_plan())
        raise AssertionError("debió lanzar PhaseError")
    except PhaseError:
        pass
    assert task.state is TaskState.FAILED


def test_approved_but_failing_tests_requests_changes(tmp_path):
    task = _make_task(tmp_path, test_command="exit 1", max_iterations=1)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED"), _ok("bien\nVERDICT: APPROVED")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    # Aprobado pero tests fallando -> corrección -> agota iteraciones -> FAILED
    assert task.state is TaskState.FAILED
    assert task.iteration == 1


def test_session_ids_are_reused(tmp_path):
    task = _make_task(tmp_path)
    impl = FakeDriver(
        "fake-impl",
        [
            RunResult(ok=True, text="v1", session_id="ses-impl"),
            RunResult(ok=True, text="v2", session_id="ses-impl"),
        ],
    )
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": impl,
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("no\nVERDICT: CHANGES_REQUESTED"), _ok("sí\nVERDICT: APPROVED")],
        ),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.sessions.get("implementer") == "ses-impl"
