"""Mini TOML serializer for the simple structures used by GRAFENO.

Supports: str, int, float, bool and nested tables (dict). Reading always uses
``tomllib`` from the stdlib; this module only covers writing, avoiding extra
dependencies for a flat configuration format.
"""

from __future__ import annotations

import re
from typing import Any

_HEADER = "# GRAFENO — generado automáticamente. Edición manual permitida.\n"

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_key(key: str) -> str:
    """TOML key: bare if safe; quoted otherwise."""
    if _BARE_KEY.match(key):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
        return f'"{escaped}"'
    raise TypeError(f"Tipo no soportado en TOML: {type(value)!r}")


def dumps(data: dict[str, Any]) -> str:
    """Serialize a dict of tables (and flat values) to TOML."""
    lines: list[str] = [_HEADER]
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for key, value in scalars.items():
        lines.append(f"{_format_key(key)} = {_format_value(value)}")
    if scalars and tables:
        lines.append("")

    for index, (name, table) in enumerate(tables.items()):
        lines.append(f"[{name}]")
        for key, value in table.items():
            lines.append(f"{_format_key(key)} = {_format_value(value)}")
        if index < len(tables) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"
