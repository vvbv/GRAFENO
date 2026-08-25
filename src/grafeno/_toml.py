"""Mini TOML serializer for the simple structures used by GRAFENO.

Supports: str, int, float, bool, nested tables (``[name]``) and arrays of
tables (``[[name]]``) at the root level. Reading always uses ``tomllib`` from
the stdlib; this module only covers writing, avoiding extra dependencies for a
flat configuration format.
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


def _dump_table(name: str, table: dict[str, Any]) -> list[str]:
    """Render a single ``[name]`` table block (lines without trailing blank)."""
    block = [f"[{name}]"]
    for key, value in table.items():
        block.append(f"{_format_key(key)} = {_format_value(value)}")
    return block


def _dump_array(name: str, items: list[Any]) -> list[str]:
    """Render a ``[[name]]`` array-of-tables block.

    Every element must be a ``dict``; anything else raises ``TypeError`` (only
    arrays of tables are supported at the root level).
    """
    block: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise TypeError(
                f"Tipo no soportado en [[{name}]]: se esperaba dict, "
                f"se obtuvo {type(item)!r}"
            )
        if index == 0:
            block.append(f"[[{name}]]")
        else:
            block.append("")
            block.append(f"[[{name}]]")
        for key, value in item.items():
            block.append(f"{_format_key(key)} = {_format_value(value)}")
    return block


def dumps(data: dict[str, Any]) -> str:
    """Serialize a dict of tables (and flat values) to TOML.

    The output preserves a stable order: root scalars first, then ``[name]``
    tables and finally ``[[name]]`` arrays-of-tables. Blocks are separated
    by a single blank line. Empty arrays emit no header.
    """
    lines: list[str] = [_HEADER]
    scalars = {k: v for k, v in data.items()
               if not isinstance(v, (dict, list))}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    arrays = {k: v for k, v in data.items()
              if isinstance(v, list) and v}

    for key, value in scalars.items():
        lines.append(f"{_format_key(key)} = {_format_value(value)}")

    blocks: list[list[str]] = []
    if scalars and (tables or arrays):
        blocks.append([])
    for name, table in tables.items():
        blocks.append(_dump_table(name, table))
    for name, items in arrays.items():
        blocks.append(_dump_array(name, items))

    for index, block in enumerate(blocks):
        if not block:
            lines.append("")
            continue
        lines.extend(block)
        if index < len(blocks) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"
