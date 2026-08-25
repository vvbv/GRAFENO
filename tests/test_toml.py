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


def test_toml_dumps_empty_array_emits_nothing():
    """Empty arrays produce no ``[[key]]`` header (just the file comment)."""
    text = _toml.dumps({"references": []})
    assert "[[references]]" not in text
    # tomllib has no way to recover the absent key, but the absence is
    # precisely what we asserted above: callers interpret missing as [].
    assert "references" not in tomllib.loads(text)


def test_toml_dumps_array_of_tables_with_other_blocks():
    """An array of tables coexists with scalars and tables in one dump."""
    data = {
        "language": "en",
        "editor": {"enabled": True, "editor": "code"},
        "references": [{"name": "a", "description": "d", "path": "/x"}],
    }
    text = _toml.dumps(data)
    assert tomllib.loads(text) == data
