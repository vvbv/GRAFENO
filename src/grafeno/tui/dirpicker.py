"""Selector de directorio con autocompletado para la TUI."""

from __future__ import annotations

import os
from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option


def directory_matches(value: str, *, limit: int = 15) -> list[str]:
    """Devuelve rutas de directorios existentes que completan ``value``.

    Reglas:
    - Solo directorios (nunca archivos).
    - ``~`` se expande al home del usuario.
    - Si ``value`` termina en separador, se listan los hijos de ese directorio.
    - Si no, se completa el último segmento por prefijo (insensible a mayúsculas).
    - Los directorios ocultos solo aparecen si el prefijo empieza por ".".
    - Las rutas devueltas conservan la forma tecleada (no se resuelven) y
      terminan en separador para permitir seguir profundizando.
    - Errores de permisos o rutas inexistentes devuelven lista vacía.
    """
    raw = value if value else "."
    expanded = os.path.expanduser(raw)
    if expanded.endswith(os.sep):
        base, prefix = expanded, ""
    else:
        head, tail = os.path.split(expanded)
        base, prefix = (head or "."), tail
    base_path = Path(base)
    if not base_path.is_dir():
        return []
    try:
        entries = list(base_path.iterdir())
    except OSError:
        return []
    prefix_fold = prefix.casefold()
    matches = [
        e
        for e in entries
        if e.is_dir()
        and e.name.casefold().startswith(prefix_fold)
        and (prefix.startswith(".") or not e.name.startswith("."))
    ]
    matches.sort(key=lambda e: e.name.casefold())
    return [str(e) + os.sep for e in matches[:limit]]


class DirectoryPicker(Widget):
    """Input de ruta con lista desplegable de directorios candidatos.

    El Input interior lleva el id que indique el llamador (p. ej. "nt-workdir");
    el desplegable se muestra al teclear y se rellena con ``directory_matches``.
    Teclas: flecha abajo (desde el input) entra en la lista; Enter/click elige;
    Escape (en la lista) vuelve al input. Tab cambia de foco como siempre.
    """

    DEFAULT_CSS = """
    DirectoryPicker {
        height: auto;
    }
    DirectoryPicker OptionList {
        height: auto;
        max-height: 8;
        display: none;
    }
    DirectoryPicker OptionList.visible {
        display: block;
    }
    """

    class Changed(Message):
        """Reenvía los cambios del Input interior (burbujea como Input.Changed)."""

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, value: str = "", *, input_id: str) -> None:
        super().__init__()
        self._input_id = input_id
        self._initial = value
        self._matches: list[str] = []

    @property
    def value(self) -> str:
        return self.query_one(Input).value

    @value.setter
    def value(self, new_value: str) -> None:
        self.query_one(Input).value = new_value

    def compose(self) -> ComposeResult:
        yield Input(value=self._initial, id=self._input_id)
        yield OptionList(id="dir-options")

    def on_mount(self) -> None:
        self._refresh_options()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != self._input_id:
            return
        self._refresh_options()
        self.post_message(self.Changed(event.value))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter o click sobre una opción: fija la ruta y vuelve al input.
        self._pick(self._matches[event.option_index])

    def _pick(self, path: str) -> None:
        input_widget = self.query_one(Input)
        input_widget.value = path
        input_widget.cursor_position = len(path)
        input_widget.focus()
        # _refresh_options() se dispara vía Input.Changed y muestra los hijos.

    def _refresh_options(self) -> None:
        options = self.query_one("#dir-options", OptionList)
        options.clear_options()
        self._matches = directory_matches(self.query_one(Input).value)
        for path in self._matches:
            options.add_option(Option(path))
        options.set_class(bool(options.option_count), "visible")

    def on_key(self, event) -> None:
        options = self.query_one("#dir-options", OptionList)
        if event.key == "down" and self.query_one(Input).has_focus and options.option_count:
            options.focus()
            options.highlighted = 0
            event.stop()
        elif event.key == "escape" and options.has_focus:
            self.query_one(Input).focus()
            event.stop()
