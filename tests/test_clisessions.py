"""Tests of the direct CLI session store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import grafeno.drivers as drivers
from grafeno import clisessions, paths, remotesession, usage
from grafeno.config import KNOWN_CLIS
from grafeno.drivers import get_driver
from grafeno.drivers.base import RunRequest


def _project(tmp_path, name="proj"):
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_create_and_load_roundtrip(tmp_path):
    project = _project(tmp_path)
    session = clisessions.create(str(project), "claude", model="sonnet")
    loaded = clisessions.load(session.id)
    assert loaded == session or loaded.to_dict()["id"] == session.id
    assert loaded.workdir == str(project.resolve())
    assert loaded.cli == "claude"
    assert loaded.model == "sonnet"
    assert loaded.name == "Session 1"
    assert loaded.state == clisessions.STATE_ACTIVE
    assert clisessions.create(str(project), "opencode").name == "Session 2"


def test_create_rejects_bad_input(tmp_path):
    project = _project(tmp_path)
    for workdir, cli in (
        (str(project), "nope"),
        ("relative/dir", "claude"),
        (str(tmp_path / "missing"), "claude"),
        ("user@host:/x", "claude"),
        ("", "claude"),
    ):
        with pytest.raises(ValueError):
            clisessions.create(workdir, cli)


def test_create_rejected_in_remote_session(monkeypatch, tmp_path):
    monkeypatch.setattr(remotesession, "active", lambda: True)
    with pytest.raises(ValueError):
        clisessions.create(str(_project(tmp_path)), "claude")


def test_list_all_filters_by_workdir_and_orders_by_update(tmp_path):
    one = _project(tmp_path, "one")
    two = _project(tmp_path, "two")
    first = clisessions.create(str(one), "claude")
    second = clisessions.create(str(two), "claude")
    third = clisessions.create(str(one), "opencode")
    # Force a deterministic order independent of the clock resolution.
    for stamp, session in (("2026-01-01T00:00:01", first), ("2026-01-01T00:00:03", second),
                           ("2026-01-01T00:00:02", third)):
        meta = paths.cli_session_dir(session.id) / "session.toml"
        text = meta.read_text(encoding="utf-8")
        meta.write_text(
            "\n".join(
                f'updated_at = "{stamp}"' if line.startswith("updated_at") else line
                for line in text.splitlines()
            ) + "\n",
            encoding="utf-8",
        )
    assert [s.id for s in clisessions.list_all()] == [second.id, third.id, first.id]
    assert [s.id for s in clisessions.list_all(str(one))] == [third.id, first.id]
    (paths.cli_sessions_dir() / "20260101-000000-abcdef").mkdir()  # no session.toml
    assert len(clisessions.list_all()) == 3


def test_transcript_append_and_incremental_load(tmp_path):
    session = clisessions.create(str(_project(tmp_path)), "claude")
    clisessions.append_entry(session.id, 1, "user", "hi")
    clisessions.append_entry(session.id, 1, "text", "hello")
    clisessions.append_entry(session.id, 1, "weird", "x")
    entries, total = clisessions.load_transcript(session.id)
    assert total == 3
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[2]["kind"] == "info"
    rest, total = clisessions.load_transcript(session.id, after=2)
    assert total == 3 and [e["seq"] for e in rest] == [3]
    with (paths.cli_session_dir(session.id) / "transcript.jsonl").open("a") as handle:
        handle.write("not json\n")
    assert clisessions.load_transcript(session.id)[1] == 3
    last, _ = clisessions.load_transcript(session.id, limit=1)
    assert [e["seq"] for e in last] == [3]
    assert clisessions.load_transcript("20260101-000000-abcdef") == ([], 0)


def test_load_rejects_invalid_ids():
    for bad in ("../x", "", "abc", "20260101-000000-ABCDEF"):
        with pytest.raises(FileNotFoundError):
            clisessions.load(bad)
    assert not clisessions.valid_id("../x")


def test_delete(tmp_path):
    session = clisessions.create(str(_project(tmp_path)), "claude")
    assert clisessions.delete(session.id) is True
    assert clisessions.delete(session.id) is False
    assert clisessions.delete("../x") is False
    with pytest.raises(FileNotFoundError):
        clisessions.load(session.id)


def test_record_session_usage_does_not_count_tasks():
    usage.record_session("/p", "claude", "sonnet", 10, 5, 7)
    summary = usage.summarize(usage.load_records())
    assert summary.tokens_in == 10 and summary.tokens_out == 5
    assert not summary.task_ids


def _task(tmp_path, cycle=1, description="desc"):
    from grafeno import models
    from grafeno.config import Config

    task = models.Task.create("T", description, str(tmp_path), Config())
    task.cycle = cycle
    return task


def _summary(task, cycle, text):
    folder = paths.final_dir(task.id, cycle)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "summary.md").write_text(text, encoding="utf-8")


def test_task_id_roundtrip(tmp_path):
    session = clisessions.create(str(_project(tmp_path)), "claude", task_id="abc")
    assert clisessions.load(session.id).task_id == "abc"
    assert clisessions.CliSession.from_dict({"id": "x"}).task_id == ""


def test_initial_context_store(tmp_path):
    session = clisessions.create(str(_project(tmp_path)), "claude")
    assert not clisessions.has_initial_context(session.id)
    clisessions.save_initial_context(session.id, "ctx")
    assert clisessions.has_initial_context(session.id)
    assert clisessions.load_initial_context(session.id) == "ctx"
    clisessions.clear_initial_context(session.id)
    clisessions.clear_initial_context(session.id)
    assert clisessions.load_initial_context(session.id) == ""


def test_latest_summary_picks_most_recent_cycle(tmp_path):
    task = _task(tmp_path, cycle=2)
    assert clisessions.latest_summary(task) is None
    _summary(task, 1, "one")
    assert clisessions.latest_summary(task) == paths.final_dir(task.id, 1) / "summary.md"
    _summary(task, 2, "two")
    assert clisessions.latest_summary(task) == paths.final_dir(task.id, 2) / "summary.md"


def test_task_context_variants(tmp_path):
    task = _task(tmp_path)
    assert "desc" in clisessions.task_context(task)
    _summary(task, 1, "RESUMEN {x} here")
    text = clisessions.task_context(task)
    assert "RESUMEN {x} here" in text and task.name in text and task.id in text
    assert text.endswith("First user message:\n")


def test_task_context_truncates(tmp_path):
    task = _task(tmp_path)
    _summary(task, 1, "a" * (clisessions.CONTEXT_MAX_CHARS + 10))
    assert "(summary truncated)" in clisessions.task_context(task)


def test_session_attachments_are_stored_in_the_session_media_dir(tmp_path):
    session = clisessions.create(str(tmp_path), "claude")
    source = tmp_path / "Informe final.xlsx"
    source.write_bytes(b"PK\x03\x04 sheet")
    saved = clisessions.save_attachment_file(session.id, source.name, source)
    assert saved is not None and saved.parent == clisessions.media_dir(session.id)
    assert saved.name == "media-01.xlsx" and source.exists()  # copied, not moved
    entries = clisessions.list_attachments(session.id)
    assert [(e.file, e.name, e.kind) for e in entries] == [("media-01.xlsx", "Informe final.xlsx", "spreadsheet")]
    assert clisessions.resolve_attachments(session.id, [saved.name, saved.name]) == entries
    block = clisessions.attachments_prompt(session.id, entries)
    assert str(saved.resolve()) in block and "Informe final.xlsx" in block


def test_unsafe_suffixes_are_stored_as_bin_and_too_many_files_are_rejected(tmp_path):
    session = clisessions.create(str(tmp_path), "claude")
    saved = clisessions.save_attachment(session.id, "evil.sh", b"rm -rf /")
    assert saved is not None and saved.suffix == ".bin"
    names = [f"media-{n:02d}.png" for n in range(1, clisessions.MAX_MESSAGE_ATTACHMENTS + 2)]
    with pytest.raises(ValueError):
        clisessions.resolve_attachments(session.id, names)
    assert clisessions.save_attachment("../escape", "a.png", b"x") is None


def test_deleting_a_session_removes_its_attachments(tmp_path):
    session = clisessions.create(str(tmp_path), "claude")
    saved = clisessions.save_attachment(session.id, "a.png", b"\x89PNG")
    assert clisessions.delete(session.id)
    assert not saved.exists()


def test_task_context_lists_task_media_with_absolute_paths(tmp_path):
    from grafeno import media

    task = _task(tmp_path)
    image = media.save_image(task.id, b"\x89PNG\r\n\x1a\n")
    doc = media.save_attachment(task.id, "notas.docx", b"PK")
    context = clisessions.task_context(task)
    assert str(image) in context and str(doc.resolve()) in context
    assert context.endswith("First user message:\n")


def _detected(monkeypatch, clis):
    monkeypatch.setattr(drivers, "available_clis", lambda: list(clis))


def test_session_clis_are_the_known_clis():
    assert set(clisessions.SESSION_CLIS) == set(KNOWN_CLIS)


@pytest.mark.parametrize("cli", KNOWN_CLIS)
def test_every_session_cli_has_a_resuming_driver(cli):
    driver = type(get_driver(cli))()  # fresh instance: never mutate the global registry
    if hasattr(driver, "_major"):
        driver._major = 1  # opencode: skip the --version spawn

    def request(session_id):
        return RunRequest(prompt="p", model="", workdir=Path("/tmp/x"), session_id=session_id, title="t")

    assert "ses_1" in driver.build_command(request("ses_1"))
    assert driver.build_command(request("ses_1")) != driver.build_command(request(None))


def test_session_clis_only_detected(monkeypatch):
    _detected(monkeypatch, ["kimi", "claude"])
    assert clisessions.session_clis() == [c for c in KNOWN_CLIS if c in ("kimi", "claude")]


def test_default_cli(monkeypatch):
    _detected(monkeypatch, ["codex", "claude"])
    assert clisessions.default_cli("codex") == "codex"
    assert clisessions.default_cli("opencode") == "claude"
    assert clisessions.default_cli() == "claude"
    _detected(monkeypatch, ["kimi"])
    assert clisessions.default_cli() == "kimi"
    _detected(monkeypatch, [])
    assert clisessions.default_cli("kimi") == "claude"


@pytest.mark.parametrize("cli", clisessions.SESSION_CLIS)
def test_create_accepts_every_supported_cli(tmp_path, cli):
    assert clisessions.create(str(tmp_path), cli).cli == cli
    with pytest.raises(ValueError):
        clisessions.create(str(tmp_path), "nope")


def test_session_with_uninstalled_cli_still_loads(tmp_path, monkeypatch):
    session = clisessions.create(str(tmp_path), "cursor")
    _detected(monkeypatch, [])
    assert clisessions.load(session.id).cli == "cursor"
    assert session.id in {s.id for s in clisessions.list_all()}
