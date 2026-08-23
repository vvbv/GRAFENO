"""Aplicación TUI de GRAFENO."""

from __future__ import annotations

from textual.app import App

from . import __version__
from .tui.screens.tasks import TaskListScreen


class GrafenoApp(App):
    TITLE = "GRAFENO"
    SUB_TITLE = f"v{__version__} · orquestador multi-CLI"
    CSS_PATH = "grafeno.tcss"

    def on_mount(self) -> None:
        self.push_screen(TaskListScreen())


def main() -> None:
    GrafenoApp().run()


if __name__ == "__main__":
    main()
