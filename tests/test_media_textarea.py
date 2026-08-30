"""Tests for the MediaTextArea widget (clipboard image paste handling)."""

from __future__ import annotations

import asyncio

from grafeno import media, paths
from grafeno.app import GrafenoApp
from grafeno.tui.widgets import MediaTextArea


def test_next_pending_name_avoids_collisions():
    pending = [("media-01.png", b""), ("media-02.png", b"")]
    assert media.next_pending_name(pending) == "media-03.png"


def test_next_pending_name_first_in_empty_list():
    assert media.next_pending_name([]) == "media-01.png"


def test_media_text_area_stores_image_with_task_id(monkeypatch):
    """With task_id, the image is saved on disk and the token is inserted."""

    async def scenario():
        png = b"\x89PNG\r\n\x1a\npayload"
        monkeypatch.setattr(media, "read_clipboard_image_async", _async_return(png))
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            area = MediaTextArea(task_id="t-area")
            await app.mount(area)
            await pilot.pause()
            ok = await area._try_paste_image()
            assert ok is True
            assert "media/media-01.png" in area.text
            listed = media.list_media("t-area")
            assert len(listed) == 1
            assert listed[0].read_bytes() == png

    asyncio.run(scenario())


def test_media_text_area_buffers_when_no_task_id(monkeypatch):
    """Without task_id, the image goes into pending and a token is still inserted."""

    async def scenario():
        png = b"\x89PNG\r\n\x1a\npayload"
        monkeypatch.setattr(media, "read_clipboard_image_async", _async_return(png))
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            area = MediaTextArea()
            await app.mount(area)
            await pilot.pause()
            ok = await area._try_paste_image()
            assert ok is True
            assert "media/media-01.png" in area.text
            assert len(area.pending) == 1
            assert area.pending[0][0] == "media-01.png"
            assert area.pending[0][1] == png

    asyncio.run(scenario())


def test_media_text_area_returns_false_without_image(monkeypatch):
    """When the clipboard has no image, the widget reports False and the text stays put."""

    async def scenario():
        monkeypatch.setattr(media, "read_clipboard_image_async", _async_return(None))
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            area = MediaTextArea(task_id="t-empty")
            await app.mount(area)
            await pilot.pause()
            assert area.text == ""
            ok = await area._try_paste_image()
            assert ok is False
            assert area.text == ""
            assert area.pending == []
            assert media.list_media("t-empty") == []

    asyncio.run(scenario())


def test_media_text_area_returns_false_on_save_failure(monkeypatch):
    """If save_image fails (_store_image returns None), no token is inserted."""

    async def scenario():
        png = b"\x89PNG\r\n\x1a\npayload"
        monkeypatch.setattr(media, "read_clipboard_image_async", _async_return(png))
        monkeypatch.setattr(media, "save_image", lambda _task_id, _data: None)
        app = GrafenoApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            area = MediaTextArea(task_id="t-fail")
            await app.mount(area)
            await pilot.pause()
            ok = await area._try_paste_image()
            assert ok is False
            assert area.text == ""

    asyncio.run(scenario())


def test_save_pending_flushes_buffered_images():
    """save_pending writes the buffered images to the task's media dir."""
    task_id = "t-flush"
    png1 = b"\x89PNG\r\n\x1a\naaaa"
    png2 = b"\x89PNG\r\n\x1a\nbbbb"
    media_dir = paths.media_dir(task_id)
    written = media.save_pending(task_id, [("media-01.png", png1), ("media-02.png", png2)])
    assert [p.name for p in written] == ["media-01.png", "media-02.png"]
    assert (media_dir / "media-01.png").read_bytes() == png1
    assert (media_dir / "media-02.png").read_bytes() == png2


def _async_return(value):
    async def _coro():
        return value

    return _coro
