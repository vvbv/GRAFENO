"""Tests of the mini TOML serializer."""

from __future__ import annotations

import tomllib

from grafeno import _toml


def test_toml_quoted_keys_roundtrip():
    data = {"tokens": {"prov/Model-X|input": 100, "plan": 3}}
    text = _toml.dumps(data)
    assert '"prov/Model-X|input" = 100' in text
    assert "plan = 3" in text  # bare keys are unchanged
    assert tomllib.loads(text) == data


def test_toml_dumps_array_of_tables():
    """Lists of dicts become ``[[key]]`` blocks parsable by tomllib."""
    data = {
        "references": [
            {"name": "a", "path": "/x"},
            {"name": "b", "path": "/y"},
        ],
    }
    text = _toml.dumps(data)
    assert "[[references]]" in text
    assert tomllib.loads(text) == data


def test_toml_dumps_empty_array_emits_no_header():
    """Empty arrays produce no ``[[key]]`` header, just an inline empty list."""
    text = _toml.dumps({"references": []})
    assert "[[references]]" not in text
    assert tomllib.loads(text)["references"] == []


def test_toml_dumps_array_of_tables_with_other_blocks():
    """An array of tables coexists with scalars and tables in one dump."""
    data = {
        "language": "en",
        "editor": {"enabled": True, "editor": "code"},
        "references": [{"name": "a", "description": "d", "path": "/x"}],
    }
    text = _toml.dumps(data)
    assert tomllib.loads(text) == data


def test_dumps_inline_string_array_roundtrip():
    """Lists of strings become inline TOML arrays readable by tomllib."""
    text = _toml.dumps({"workspaces": ["~/Documents/GitHub", "/opt/code"], "language": "en"})
    assert 'workspaces = ["~/Documents/GitHub", "/opt/code"]' in text
    data = tomllib.loads(text)
    assert data["workspaces"] == ["~/Documents/GitHub", "/opt/code"]
    assert data["language"] == "en"


def test_dumps_empty_array_and_table_array_still_work():
    """Empty lists stay inline while lists of dicts keep the ``[[name]]`` form."""
    text = _toml.dumps({"workspaces": [], "references": [{"title": "a", "url": "b"}]})
    data = tomllib.loads(text)
    assert data["workspaces"] == []
    assert data["references"] == [{"title": "a", "url": "b"}]
    assert "[[references]]" in text
