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
