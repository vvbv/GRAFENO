"""Tests for grafeno.mdnorm (Markdown normalization)."""

from __future__ import annotations

from grafeno.mdnorm import normalize_markdown


def test_tightens_loose_bullet_list():
    assert normalize_markdown("- a\n\n- b\n\n- c\n") == "- a\n- b\n- c\n"


def test_tightens_loose_ordered_list():
    assert normalize_markdown("1. a\n\n2. b\n") == "1. a\n2. b\n"


def test_tightens_checkbox_list():
    assert normalize_markdown("- [ ] a\n\n- [x] b\n") == "- [ ] a\n- [x] b\n"


def test_keeps_blank_line_between_paragraph_and_list():
    assert normalize_markdown("texto\n\n- a\n") == "texto\n\n- a\n"


def test_keeps_blank_line_between_list_and_paragraph():
    assert normalize_markdown("- a\n\ntexto\n") == "- a\n\ntexto\n"


def test_collapses_multiple_blank_lines():
    assert normalize_markdown("a\n\n\n\nb\n") == "a\n\nb\n"


def test_preserves_blanks_inside_code_fence():
    source = "```python\ndef a():\n    pass\n\n\ndef b():\n    pass\n```\n"
    assert normalize_markdown(source) == source


def test_normalizes_crlf():
    assert normalize_markdown("- a\r\n\r\n- b\r\n") == "- a\n- b\n"


def test_does_not_merge_nested_sublists():
    assert normalize_markdown("- a\n\n  - b\n") == "- a\n\n  - b\n"


def test_idempotent():
    source = "- a\n\n- b\n\n\n\n```\ncode\n\n\ncode\n```\n\ntexto\n"
    once = normalize_markdown(source)
    assert normalize_markdown(once) == once


def test_empty_string():
    assert normalize_markdown("") == ""
