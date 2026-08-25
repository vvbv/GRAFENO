"""Driver para Codex CLI (https://github.com/openai/codex).

Modo no-interactivo (verificado contra codex-cli 0.133.0):
    codex exec --json -s workspace-write --skip-git-repo-check \
        -C <workdir> [-m <model>] [-c model_reasoning_effort="<nivel>"] "<prompt>"

- ``--json`` emite eventos JSONL por stdout.
- ``-s workspace-write`` permite escribir en el proyecto sin aprobaciones
  (``codex exec`` no pregunta; el sandbox es la única barrera).
- Modelo con ``-m``; nivel de trabajo con
  ``-c model_reasoning_effort="<nivel>"``.
- Directorio de trabajo con ``-C``.
- Continuación de sesión: ``codex exec [opciones] resume <session_id> "<prompt>"``
  (las opciones de ``exec`` van ANTES del subcomando ``resume``).
- No expone comando de listado de modelos: lista estática (mejor esfuerzo).

Eventos JSONL reales verificados:
  ``{"type":"thread.started","thread_id":"..."}``
  ``{"type":"item.completed","item":{"type":"agent_message","text":"..."}}``
  ``{"type":"item.completed","item":{"type":"command_execution","command":"..."}}``
  ``{"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}``
  ``{"type":"error","message":"..."}``

Notas verificadas: ``usage`` puede incluir ``cached_input_tokens`` y
``reasoning_output_tokens`` que se IGNORAN (solo input/output directos).
El id de sesión viaja en ``thread_id``.
"""

from __future__ import annotations

from typing import Any

from .base import CLIDriver, EventKind, RunEvent, RunRequest, TokenUsage


class CodexDriver(CLIDriver):
    name = "codex"
    display_name = "Codex CLI"
    executable = "codex"
    # codex no tiene comando init nativo en modo no-interactivo: prompt genérico.
    init_command = ""

    # codex no expone listado de modelos: lista estática (mejor esfuerzo).
    STATIC_MODELS = ("gpt-5.1-codex-max", "gpt-5.1-codex", "gpt-5.1-codex-mini")
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh")

    def build_command(self, request: RunRequest) -> list[str]:
        command = [
            "codex", "exec", "--json", "-s", "workspace-write",
            "--skip-git-repo-check",
        ]
        if request.model:
            command += ["-m", request.model]
        if request.effort:
            # El valor debe llevar comillas para que TOML lo parsee como string.
            command += ["-c", f'model_reasoning_effort="{request.effort}"']
        command += ["-C", str(request.workdir)]
        if request.session_id:
            # Las opciones de exec van antes del subcomando resume.
            command += ["resume", request.session_id, request.prompt]
        else:
            command.append(request.prompt)
        return command

    def list_models(self) -> list[str]:
        return sorted(self.STATIC_MODELS)

    async def list_models_async(self, timeout: float = 30.0) -> list[str]:
        return self.list_models()  # sin subproceso: lista estática

    async def list_variants_async(self, timeout: float = 30.0) -> dict[str, list[str]]:
        """Variantes estáticas: codex no expone comando para listarlas."""
        return {model: list(self.EFFORT_LEVELS) for model in self.list_models()}

    # ------------------------------------------------------------ #
    def decode_event(self, payload: dict[str, Any]) -> tuple[RunEvent | None, str | None]:
        session_id = (
            payload.get("thread_id")
            or payload.get("session_id")
            or payload.get("sessionId")
        )
        event_type = str(payload.get("type", ""))

        if event_type == "item.completed":
            event = self._decode_item(payload.get("item") or {})
            return event, session_id

        if event_type in {
            "thread.started", "turn.started", "turn.completed",
            "item.started", "item.updated",
        }:
            return None, session_id  # ruido interno: se registra en el log crudo

        if event_type == "error" or payload.get("error"):
            message = payload.get("message") or payload.get("error") or str(payload)
            return RunEvent(EventKind.ERROR, str(message)[:500]), session_id

        return RunEvent(EventKind.INFO, f"[{event_type or 'evento'}]"), session_id

    @staticmethod
    def _decode_item(item: dict[str, Any]) -> RunEvent | None:
        """Interpreta el ``item`` de un evento ``item.completed``."""
        item_type = str(item.get("type") or item.get("item_type") or "")
        if item_type in {"agent_message", "assistant_message"}:
            text = str(item.get("text") or "").strip()
            return RunEvent(EventKind.TEXT, text) if text else None
        if item_type in {"command_execution", "local_shell_call", "shell_command"}:
            summary = str(item.get("command") or item_type)
            return RunEvent(EventKind.TOOL, summary[:200])
        return None  # reasoning, file_change, ...: solo al log crudo

    # ------------------------------------------------------------ #
    def extract_usage(self, payload: dict[str, Any]) -> TokenUsage | None:
        if str(payload.get("type", "")) != "turn.completed":
            return None
        usage_dict = payload.get("usage")
        if not isinstance(usage_dict, dict):
            return None
        try:
            usage = TokenUsage(
                input=int(usage_dict.get("input_tokens") or 0),
                # Se ignoran cached/reasoning tokens: solo input/output directos.
                output=int(usage_dict.get("output_tokens") or 0),
            )
        except (TypeError, ValueError):
            return None
        return None if usage.empty else usage
