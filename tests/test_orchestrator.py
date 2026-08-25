"""Tests of the orchestrator with injected fake drivers."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest

from grafeno import models, paths
from grafeno.config import Config
from grafeno.drivers.base import CLIDriver, RunResult, TokenUsage
from grafeno.models import Task, TaskState
from grafeno.pipeline import prompts
from grafeno.pipeline.orchestrator import Orchestrator, PhaseError


class FakeDriver(CLIDriver):
    """Test driver: returns queued results, no subprocesses."""

    def __init__(self, name: str, results: list[RunResult], available: bool = True):
        self.name = name
        self.display_name = name
        self.executable = name
        self._available = available
        self._results = deque(results)
        self.prompts: list[str] = []
        self.requests: list = []

    def is_available(self) -> bool:
        return self._available

    def build_command(self, request):
        return []

    def list_models(self):
        return []

    async def run(self, request, on_event=None, on_activity=None):
        self.prompts.append(request.prompt)
        self.requests.append(request)
        if request.log_path is not None:
            request.log_path.parent.mkdir(parents=True, exist_ok=True)
            with request.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{request.prompt[:80]}\n")
        if on_activity:
            on_activity()
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
    task.final.cli = "fake-final"
    for key, value in overrides.items():
        setattr(task, key, value)
    return task


def _run(coro):
    return asyncio.run(coro)


def test_automode_happy_path(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("init"), _ok("contenido del plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("implementado")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("Todo bien.\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 0
    # The plan was materialised with the executor header (output fallback).
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
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.iteration == 1
    # The fix received the fix prompt that references review 01.
    assert "01-review.md" in drivers["fake-impl"].prompts[-1]


def test_automode_max_iterations_exhausted(tmp_path):
    task = _make_task(tmp_path, max_iterations=2)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", []),  # always ok
        "fake-rev": FakeDriver("fake-rev", []),    # no verdict -> CHANGES_REQUESTED
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
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
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
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
    task.planner.cli = "inexistente"  # unknown CLI: the orchestrator fails
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
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    # Approved but tests failing -> fix -> exhausts iterations -> FAILED
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
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.sessions.get("implementer") == "ses-impl"


def test_automode_split_plan_and_continue(tmp_path):
    """Automode with a confirmation point: plan -> (pause) -> continue."""
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)

    _run(orch.run_automode_plan())
    assert task.state is TaskState.PLANNED
    assert list(paths.plan_dir(task.id).glob("*.md"))
    # Nothing implemented yet.
    assert drivers["fake-impl"].prompts == []

    _run(orch.run_automode_continue())
    assert task.state is TaskState.DONE
    assert len(drivers["fake-impl"].prompts) == 1


def test_automode_continue_requires_plan(tmp_path):
    task = _make_task(tmp_path)
    infos: list[str] = []
    orch = Orchestrator(task, drivers={}, on_info=infos.append)
    _run(orch.run_automode_continue())
    assert task.state is TaskState.DRAFT
    assert any("plan" in message.lower() for message in infos)


def test_second_cycle_uses_cycle_dirs(tmp_path):
    """'Ask for more': a new cycle plans in plan/ciclo-02 and keeps cycle 1."""
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("plan ciclo 1"), _ok("plan ciclo 2")])
    drivers = {
        "fake-planner": planner,
        "fake-impl": FakeDriver("fake-impl", [_ok("impl 1"), _ok("impl 2")]),
        "fake-rev": FakeDriver(
            "fake-rev", [_ok("ok\nVERDICT: APPROVED"), _ok("ok\nVERDICT: APPROVED")]
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre"), _ok("cierre 2")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.DONE
    assert list(paths.plan_dir(task.id, 1).glob("*.md"))

    task.start_new_cycle("añade más cosas")
    models.save(task)
    assert task.cycle == 2
    assert task.iteration == 0
    assert task.state is TaskState.DRAFT
    assert task.current_extension == "añade más cosas"

    orch2 = Orchestrator(task, drivers=drivers)
    _run(orch2.run_automode())
    assert task.state is TaskState.DONE

    cycle2_plans = list(paths.plan_dir(task.id, 2).glob("*.md"))
    assert cycle2_plans and "ciclo-02" in str(cycle2_plans[0])
    assert list(paths.review_dir(task.id, 2).glob("*.md"))
    # The cycle 2 prompt includes the extension request.
    assert "añade más cosas" in planner.prompts[-1]
    assert "Ampliación" in planner.prompts[-1]
    # Cycle 1 remains untouched.
    assert list(paths.plan_dir(task.id, 1).glob("*.md"))
    cycle2_final = list(paths.final_dir(task.id, 2).glob("*.md"))
    assert cycle2_final and "ciclo-02" in str(cycle2_final[0])


def test_agents_md_se_genera_antes_del_plan(tmp_path):
    """Without AGENTS.md in the project, the planner generates it before planning."""
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("init hecho"), _ok("plan")])
    drivers = {
        "fake-planner": planner,
        "fake-impl": FakeDriver("fake-impl", []),
        "fake-rev": FakeDriver("fake-rev", []),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert len(planner.prompts) == 2
    assert "AGENTS.md" in planner.prompts[0]   # first the init
    assert "PLANIFICADOR" in planner.prompts[1]  # then the plan


def test_agents_md_se_omite_si_ya_existe(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Agentes\n", encoding="utf-8")
    task = _make_task(tmp_path)
    planner = FakeDriver("fake-planner", [_ok("plan")])
    drivers = {"fake-planner": planner}
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert len(planner.prompts) == 1  # only the plan prompt


def test_agents_md_fallo_no_falla_la_tarea(tmp_path):
    """If AGENTS.md generation fails, the plan continues."""
    task = _make_task(tmp_path)
    planner = FakeDriver(
        "fake-planner", [RunResult(ok=False, error="boom"), _ok("plan")]
    )
    drivers = {"fake-planner": planner}
    infos: list[str] = []
    orch = Orchestrator(task, drivers=drivers, on_info=infos.append)
    _run(orch.run_plan())

    assert task.state is TaskState.PLANNED
    assert any("AGENTS.md" in message for message in infos)


def test_durations_are_recorded_and_persisted(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2")]),
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("no\nVERDICT: CHANGES_REQUESTED"), _ok("sí\nVERDICT: APPROVED")],
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    activities: list[str] = []
    orch = Orchestrator(task, drivers=drivers, on_activity=lambda phase: activities.append(phase))
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    # Every phase the task went through has a recorded duration.
    for phase in ("plan", "implement", "review", "fix", "final"):
        assert phase in task.durations
        assert task.durations[phase] >= 0
    # The activity callback propagates.
    assert activities
    # Persisted in task.toml.
    from grafeno import models

    reloaded = models.load(task.id)
    assert reloaded.durations == task.durations


def test_automode_runs_final_steps_after_approval(tmp_path):
    """After approval, automode runs the final steps and writes the report."""
    task = _make_task(tmp_path)
    final = FakeDriver("fake-final", [_ok("informe de cierre")])
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": final,
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert len(final.prompts) == 1
    # The final prompt points to the cycle's final directory.
    assert str(paths.final_dir(task.id)) in final.prompts[0]
    # Fallback: the output was materialised as 01-final.md.
    report = paths.final_dir(task.id) / "01-final.md"
    assert report.exists()
    assert "informe de cierre" in report.read_text(encoding="utf-8")
    assert "final" in task.durations


def test_final_steps_failure_marks_failed(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1")]),
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [RunResult(ok=False, error="boom")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())
    assert task.state is TaskState.FAILED


def test_tokens_recorded_per_phase_and_cli_model(tmp_path):
    task = _make_task(tmp_path)
    task.implementer.model = "prov/Impl-Model"
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [
            RunResult(ok=True, text="impl", tokens=TokenUsage(input=1000, output=200)),
        ]),
        "fake-rev": FakeDriver("fake-rev", [_ok("VERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    assert task.state is TaskState.DONE
    assert task.tokens_by_phase()["implement"] == (1000, 200)
    assert task.tokens_by_cli_model()["fake-impl/prov/Impl-Model"] == (1000, 200)
    assert task.token_totals() == (1000, 200)
    # Persisted: it is reloaded from task.toml without loss.
    assert models.load(task.id).tokens == task.tokens


def test_tokens_accumulate_per_phase(tmp_path):
    task = _make_task(tmp_path)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [
            RunResult(ok=True, text="plan", tokens=TokenUsage(input=100, output=10)),
        ]),
        "fake-impl": FakeDriver("fake-impl", [
            RunResult(ok=True, text="impl", tokens=TokenUsage(input=1000, output=200)),
        ]),
        "fake-rev": FakeDriver("fake-rev", [
            RunResult(ok=True, text="VERDICT: APPROVED", tokens=TokenUsage(input=50, output=5)),
        ]),
        "fake-final": FakeDriver("fake-final", [
            RunResult(ok=True, text="cierre", tokens=TokenUsage(input=30, output=3)),
        ]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    by_phase = task.tokens_by_phase()
    assert by_phase["plan"] == (100, 10)
    assert by_phase["implement"] == (1000, 200)
    assert by_phase["review"] == (50, 5)
    assert by_phase["final"] == (30, 3)


def test_hooks_fire_per_phase_and_on_each_iteration(tmp_path):
    task = _make_task(
        tmp_path,
        hook_command='echo "$GRAFENO_PHASE:$GRAFENO_OUTCOME" >> hooks.log',
        hook_stages="implement,review,fix",
    )
    models.save(task)
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": FakeDriver("fake-impl", [_ok("v1"), _ok("v2")]),
        "fake-rev": FakeDriver(
            "fake-rev",
            [_ok("Mal.\nVERDICT: CHANGES_REQUESTED"), _ok("Bien.\nVERDICT: APPROVED")],
        ),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_automode())

    lines = (tmp_path / "hooks.log").read_text(encoding="utf-8").splitlines()
    # review and fix repeat per iteration: the hook fires on each one.
    assert lines.count("review:ok") == 2
    assert lines.count("fix:ok") == 1
    assert "implement:ok" in lines
    assert all(line.endswith(":ok") for line in lines)


def test_hook_fires_with_failed_outcome(tmp_path):
    task = _make_task(
        tmp_path,
        hook_command='echo "$GRAFENO_PHASE:$GRAFENO_OUTCOME" >> hooks.log',
        hook_stages="implement",
    )
    models.save(task)
    drivers = {
        "fake-impl": FakeDriver("fake-impl", [RunResult(ok=False, text="", error="boom")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    with pytest.raises(PhaseError):
        _run(orch.run_implement())
    lines = (tmp_path / "hooks.log").read_text(encoding="utf-8").splitlines()
    assert lines == ["implement:failed"]


def test_repetition_runner_reuse_calls_run_automode(tmp_path):
    from grafeno.pipeline.orchestrator import repetition_runner

    task = _make_task(tmp_path, plan_reuse="reuse")
    runner = repetition_runner(task)
    captured = []

    async def fake_run(orch):
        captured.append("run_automode")

    class _Orch:
        run_automode = fake_run

    _run(runner(_Orch()))
    assert captured == ["run_automode"]


def test_repetition_runner_replan_calls_run_automode(tmp_path):
    from grafeno.pipeline.orchestrator import repetition_runner

    task = _make_task(tmp_path, plan_reuse="replan")
    runner = repetition_runner(task)
    captured = []

    async def fake_run(orch):
        captured.append("run_automode")

    class _Orch:
        run_automode = fake_run

    _run(runner(_Orch()))
    assert captured == ["run_automode"]


def test_repetition_runner_reevaluate_runs_reevaluate_then_continue(tmp_path):
    """With plan_reuse=reevaluate, first re-evaluate the plan and then run the rest."""
    from grafeno.pipeline.orchestrator import repetition_runner

    task = _make_task(tmp_path, plan_reuse="reevaluate")
    runner = repetition_runner(task)
    captured = []

    class _Orch:
        def __init__(self):
            self.task = task

        async def run_reevaluate_plan(self):
            captured.append("reevaluate")

        async def run_automode_continue(self):
            captured.append("continue")

    _run(runner(_Orch()))
    assert captured == ["reevaluate", "continue"]


def test_repetition_runner_reevaluate_stops_when_plan_failed(tmp_path):
    """If the re-evaluation fails (state=FAILED), the continuation is not run."""
    from grafeno.pipeline.orchestrator import repetition_runner

    task = _make_task(tmp_path, plan_reuse="reevaluate")
    runner = repetition_runner(task)
    captured = []

    class _Orch:
        def __init__(self):
            self.task = task
            self.task.state = TaskState.FAILED

        async def run_reevaluate_plan(self):
            captured.append("reevaluate")

        async def run_automode_continue(self):
            captured.append("continue")

    _run(runner(_Orch()))
    assert captured == ["reevaluate"]


def test_run_reevaluate_plan_without_existing_files_falls_back_to_run_plan(tmp_path):
    """Without plan files, run_reevaluate_plan delegates to run_plan."""
    task = _make_task(tmp_path, plan_reuse="reevaluate")
    planner = FakeDriver("fake-planner", [_ok("plan nuevo")])
    drivers = {"fake-planner": planner}
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_reevaluate_plan())

    assert task.state is TaskState.PLANNED
    # plan.jsonl was used, not reevaluate.jsonl.
    assert (paths.logs_dir(task.id) / "plan.jsonl").exists()
    assert not (paths.logs_dir(task.id) / "reevaluate.jsonl").exists()


def test_run_reevaluate_plan_with_existing_files_writes_reevaluate_log(tmp_path):
    """With an existing plan, run_reevaluate_plan writes reevaluate.jsonl."""
    task = _make_task(tmp_path, plan_reuse="reevaluate")
    # Create a previous plan file.
    (paths.plan_dir(task.id) / "01-previo.md").write_text(
        f"{prompts.executor_header(task)}\n# Plan previo\n", encoding="utf-8"
    )
    planner = FakeDriver("fake-planner", [_ok("plan reevaluado")])
    drivers = {"fake-planner": planner}
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_reevaluate_plan())

    assert task.state is TaskState.PLANNED
    # Re-evaluation specific log (not plan).
    assert (paths.logs_dir(task.id) / "reevaluate.jsonl").exists()
    assert not (paths.logs_dir(task.id) / "plan.jsonl").exists()
    # The planner received the re-evaluation prompt.
    assert "REEVALUACIÓN" in planner.prompts[-1]


def test_effort_is_passed_to_run_request(tmp_path):
    """The orchestrator propagates ``effort`` from the role to ``RunRequest``."""
    task = _make_task(tmp_path)
    task.implementer.effort = "high"
    impl = FakeDriver("fake-impl", [_ok("v1")])
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": impl,
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_implement())

    assert impl.requests, "FakeDriver should have received at least one RunRequest"
    assert impl.requests[0].effort == "high"


def test_effort_empty_is_passed_through(tmp_path):
    """Without a configured effort, ``RunRequest.effort`` is the empty string."""
    task = _make_task(tmp_path)
    assert task.implementer.effort == ""
    impl = FakeDriver("fake-impl", [_ok("v1")])
    drivers = {
        "fake-planner": FakeDriver("fake-planner", [_ok("plan")]),
        "fake-impl": impl,
        "fake-rev": FakeDriver("fake-rev", [_ok("bien\nVERDICT: APPROVED")]),
        "fake-final": FakeDriver("fake-final", [_ok("cierre")]),
    }
    orch = Orchestrator(task, drivers=drivers)
    _run(orch.run_implement())

    assert impl.requests[0].effort == ""
