"""Tests of the direct CLI session store."""

from __future__ import annotations

import json

import pytest

from grafeno import clisessions, paths, remotesession, usage


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
        (str(project), "kimi"),
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
