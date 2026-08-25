"""Tests for the gh (GitHub CLI) integration module."""

from __future__ import annotations

from pathlib import Path

from grafeno import gh


def test_gh_available_false_without_binary(monkeypatch):
    monkeypatch.setattr(gh.shutil, "which", lambda _name: None)
    assert gh.gh_available(Path("/tmp")) is False


def test_gh_available_false_outside_repo(monkeypatch):
    monkeypatch.setattr(gh.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(gh, "is_git_repo", lambda _wd: False)
    assert gh.gh_available(Path("/tmp")) is False


def test_gh_available_false_without_access(monkeypatch):
    monkeypatch.setattr(gh.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(gh, "is_git_repo", lambda _wd: True)
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (False, ""))
    assert gh.gh_available(Path("/tmp")) is False


def test_gh_available_true(monkeypatch):
    monkeypatch.setattr(gh.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(gh, "is_git_repo", lambda _wd: True)
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (True, '{"name": "GRAFENO"}'))
    assert gh.gh_available(Path("/tmp")) is True


def test_list_issues_parses_json(monkeypatch):
    payload = (
        '[{"number": 7, "title": "Fix login", "body": "Steps to reproduce"},'
        ' {"number": 3, "title": "Docs", "body": null}]'
    )
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (True, payload))
    issues = gh.list_issues(Path("/tmp"))
    assert [i.number for i in issues] == [7, 3]
    assert issues[0].title == "Fix login"
    assert issues[1].body == ""  # null body becomes empty string


def test_list_issues_empty_on_command_error(monkeypatch):
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (False, "auth required"))
    assert gh.list_issues(Path("/tmp")) == []


def test_list_issues_empty_on_invalid_json(monkeypatch):
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (True, "not json"))
    assert gh.list_issues(Path("/tmp")) == []


def test_list_issues_skips_malformed_entries(monkeypatch):
    payload = '[{"number": 1, "title": "Ok", "body": ""}, {"title": "sin numero"}]'
    monkeypatch.setattr(gh, "_gh", lambda _wd, *args: (True, payload))
    issues = gh.list_issues(Path("/tmp"))
    assert len(issues) == 1
    assert issues[0].number == 1
