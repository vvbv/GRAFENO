"""Tests del parseo de veredictos."""

from __future__ import annotations

from grafeno.pipeline.verdict import Verdict, parse_verdict


def test_approved():
    assert parse_verdict("Todo correcto.\nVERDICT: APPROVED") is Verdict.APPROVED


def test_changes_requested():
    assert parse_verdict("Faltan cosas.\nVERDICT: CHANGES_REQUESTED") is Verdict.CHANGES_REQUESTED


def test_tolerates_markdown_bold():
    assert parse_verdict("**VERDICT: APPROVED**") is Verdict.APPROVED
    assert parse_verdict("VERDICT: **CHANGES_REQUESTED**") is Verdict.CHANGES_REQUESTED


def test_last_match_wins():
    text = "VERDICT: CHANGES_REQUESTED\n... tras releer ...\nVERDICT: APPROVED"
    assert parse_verdict(text) is Verdict.APPROVED


def test_missing_verdict():
    assert parse_verdict("sin veredicto") is None
    assert parse_verdict("") is None
    assert parse_verdict(None) is None
