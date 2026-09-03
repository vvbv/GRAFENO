"""Tests of the orange update hint in the header."""

from __future__ import annotations

import asyncio

from grafeno.app import GrafenoApp
from grafeno.tui.widgets import GrafenoHeader


def test_header_shows_orange_hint_when_update_available():
    async def scenario():
        app = GrafenoApp()
        async with app.run_test(size=(110, 80)) as pilot:
            await pilot.pause()
            header = app.screen.query_one(GrafenoHeader)
            assert "available" not in str(header.format_title())
            app.available_update = "9.9.9"
            await pilot.pause()
            rendered = str(header.format_title())
            assert "(v9.9.9 available)" in rendered

    asyncio.run(scenario())