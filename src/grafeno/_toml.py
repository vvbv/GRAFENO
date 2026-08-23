"""Mini serializador TOML para las estructuras simples de GRAFENO.

Soporta: str, int, float, bool y tablas anidadas (dict). La lectura se hace
siempre con ``tomllib`` de la stdlib; este módulo solo cubre la escritura,
evitando dependencias extra para un formato de configuración plano.
"""

from __future__ import annotations

from typing import Any

_HEADER = "# GRAFENO — generado automáticamente. Edición manual permitida.\n"


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
    """Serializa un dict de tablas (y valores planos) a TOML."""
    lines: list[str] = [_HEADER]
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}

    for key, value in scalars.items():
        lines.append(f"{key} = {_format_value(value)}")
    if scalars and tables:
        lines.append("")

    for index, (name, table) in enumerate(tables.items()):
        lines.append(f"[{name}]")
        for key, value in table.items():
            lines.append(f"{key} = {_format_value(value)}")
        if index < len(tables) - 1:
            lines.append("")

    return "\n".join(lines) + "\n"
