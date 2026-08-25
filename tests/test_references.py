"""Tests of the references module and its persistence."""

from __future__ import annotations

from pathlib import Path

from grafeno import models, references
from grafeno.config import Config, PROJECT_CONFIG_FILE
from grafeno.references import Reference


def test_reference_dict_roundtrip():
    """Reference roundtrips through to_dict/from_dict."""
    ref = Reference(name="r1", description="a description", path="/tmp/x")
    data = ref.to_dict()
    assert data == {"name": "r1", "description": "a description", "path": "/tmp/x"}
    assert Reference.from_dict(data) == ref


def test_reference_from_dict_defaults():
    """Missing keys default to empty strings."""
    ref = Reference.from_dict({})
    assert ref == Reference(name="", description="", path="")


def test_save_load_global_roundtrip(tmp_path):
    """Saving and reloading returns the same list of references."""
    refs = [
        Reference(name="r1", description="first", path="/x"),
        Reference(name="r2", path="https://example.com"),
    ]
    references.save_global(refs)
    loaded = references.load_global()
    assert loaded == refs


def test_load_global_missing_returns_empty(tmp_path):
    """If the global file does not exist, returns []."""
    assert references.load_global() == []


def test_load_project_with_references_and_editor(tmp_path):
    """The ``[[references]]`` section coexists with the ``[editor]`` section."""
    from grafeno import _toml

    payload = {
        "editor": {"enabled": True, "editor": "code", "mode": "window", "side": "left"},
        "references": [
            {"name": "p1", "description": "p1d", "path": "/p1"},
            {"name": "p2", "path": "https://p2"},
        ],
    }
    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps(payload), encoding="utf-8")
    loaded = references.load_project(tmp_path)
    assert loaded == [
        Reference(name="p1", description="p1d", path="/p1"),
        Reference(name="p2", description="", path="https://p2"),
    ]


def test_load_project_missing_returns_empty(tmp_path):
    """If the project config does not exist, returns []."""
    assert references.load_project(tmp_path) == []


def test_load_project_with_corrupt_file_returns_empty(tmp_path):
    """A corrupt .grafeno.toml yields an empty list (tolerant pattern)."""
    (tmp_path / PROJECT_CONFIG_FILE).write_text("not valid toml [[[", encoding="utf-8")
    assert references.load_project(tmp_path) == []


def test_resolve_combines_levels_in_order(tmp_path):
    """Global + project + own, honoring exclusion flags, in that order."""
    references.save_global([
        Reference(name="g1", path="/g1"),
        Reference(name="g2", path="/g2"),
    ])
    from grafeno import _toml

    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps({
        "references": [Reference(name="p1", path="/p1").to_dict()],
    }), encoding="utf-8")
    cfg = Config()
    task = models.Task.create(
        "Demo", "desc", str(tmp_path), cfg,
        references=[Reference(name="t1", path="/t1")],
    )

    resolved = references.resolve(task)
    assert [r.name for r in resolved] == ["g1", "g2", "p1", "t1"]


def test_resolve_excludes_global_when_disabled(tmp_path):
    """``use_global_references=False`` drops the global references."""
    references.save_global([Reference(name="g1", path="/g1")])
    cfg = Config()
    task = models.Task.create(
        "Demo", "desc", str(tmp_path), cfg,
        use_global_references=False,
        references=[Reference(name="t1", path="/t1")],
    )
    resolved = references.resolve(task)
    assert [r.name for r in resolved] == ["t1"]


def test_resolve_excludes_project_when_disabled(tmp_path):
    """``use_project_references=False`` drops the project references."""
    from grafeno import _toml

    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps({
        "references": [Reference(name="p1", path="/p1").to_dict()],
    }), encoding="utf-8")
    cfg = Config()
    task = models.Task.create(
        "Demo", "desc", str(tmp_path), cfg,
        use_project_references=False,
        references=[Reference(name="t1", path="/t1")],
    )
    resolved = references.resolve(task)
    assert [r.name for r in resolved] == ["t1"]


def test_resolve_excludes_both_levels_when_disabled(tmp_path):
    """Both flags off: only the task-level references remain."""
    references.save_global([Reference(name="g1", path="/g1")])
    from grafeno import _toml

    (tmp_path / PROJECT_CONFIG_FILE).write_text(_toml.dumps({
        "references": [Reference(name="p1", path="/p1").to_dict()],
    }), encoding="utf-8")
    cfg = Config()
    task = models.Task.create(
        "Demo", "desc", str(tmp_path), cfg,
        use_global_references=False,
        use_project_references=False,
        references=[Reference(name="t1", path="/t1")],
    )
    resolved = references.resolve(task)
    assert [r.name for r in resolved] == ["t1"]
