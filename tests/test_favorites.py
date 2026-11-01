"""Tests of the server-side favorite projects store (favorites.py)."""

from __future__ import annotations

from grafeno import favorites, paths


def test_load_without_file_is_empty() -> None:
    assert favorites.load() == []


def test_set_favorite_marks_without_duplicates_and_unmarks() -> None:
    assert favorites.set_favorite("/a/b/", True) == ["/a/b"]
    assert favorites.set_favorite("/a/b", True) == ["/a/b"]
    assert favorites.load() == ["/a/b"]
    assert favorites.set_favorite("/a/b", False) == []
    assert favorites.load() == []


def test_corrupt_file_is_ignored() -> None:
    paths.project_favorites_path().write_text("not = [toml", encoding="utf-8")
    assert favorites.load() == []


def test_invalid_entries_are_ignored() -> None:
    paths.project_favorites_path().write_text('favorites = ["/x", "", 3]\n', encoding="utf-8")
    assert favorites.load() == ["/x"]
