"""Global settings screen (planner / implementer / reviewer / automode)."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from ... import config as config_module
from ... import editor as editor_module
from ... import paths
from ... import references as references_module
from ... import triggers as triggers_module
from ...config import KNOWN_CLIS, Config
from ...drivers import fetch_all_models, fetch_all_variants
from ...i18n import LANGUAGES, set_language, t
from ...pipeline.hooks import HOOK_STAGES, format_stages, parse_stages
from ..refform import ReferencesForm
from ..rolesform import ROLES, RolesForm
from ..trigform import TriggersForm
from ..widgets import GrafenoHeader


class ConfigScreen(Screen[None]):
    BINDINGS = [Binding("escape", "back", t("common.back"))]

    def compose(self) -> ComposeResult:
        yield GrafenoHeader()
        with Vertical(id="config-container"):
            yield Label(t("cfg.title", path=paths.config_path()), id="config-title")
            yield Static(t("cfg.roles"), classes="section-title")
            yield RolesForm()
            yield Static(t("cfg.models.loading"), id="models-status")
            yield Static(t("cfg.automode"), classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Checkbox(t("cfg.am.enabled"), id="am-enabled")
                yield Checkbox(t("cfg.am.branch"), id="am-branch")
            with Horizontal(classes="automode-row"):
                yield Checkbox(t("cfg.am.confirm_plan"), id="am-confirm-plan")
            with Horizontal(classes="automode-row"):
                yield Label(t("cfg.max_iter"))
                yield Input(id="am-max-iter", type="integer", placeholder="5")
                yield Label(t("cfg.tests"))
                yield Input(id="am-tests", placeholder="p. ej. pytest -q")
            yield Static(t("cfg.updates"), classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Checkbox(t("cfg.upd.enabled"), id="upd-enabled")
            yield Label(t("cfg.final_prompt"))
            yield TextArea(id="cfg-final-prompt")
            yield Static(t("cfg.hook"), classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Label(t("cfg.hook.command"))
                yield Input(id="hook-command", placeholder=t("hook.placeholder"))
            yield Static(t("hook.help"))
            yield Label(t("cfg.hook.stages"))
            with Horizontal(classes="automode-row", id="hook-stages"):
                for stage in HOOK_STAGES:
                    yield Checkbox(t(f"hook.stage.{stage}"), id=f"hook-stage-{stage}")
            yield Static(t("cfg.editor"), classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Checkbox(t("cfg.editor.enabled"), id="editor-enabled")
            with Horizontal(classes="automode-row"):
                yield Label(t("cfg.editor.name"))
                yield Select([], id="editor-name")
                yield Label(t("cfg.editor.mode"))
                yield Select(
                    [(t("cfg.editor.mode.window"), "window"),
                     (t("cfg.editor.mode.split"), "split"),
                     (t("cfg.editor.mode.none"), "none")],
                    id="editor-mode",
                    allow_blank=False,
                )
                yield Label(t("cfg.editor.side"))
                yield Select(
                    [(t("cfg.editor.side.left"), "left"),
                     (t("cfg.editor.side.right"), "right")],
                    id="editor-side",
                    allow_blank=False,
                )
            yield Static(t("cfg.language"), classes="section-title")
            with Horizontal(classes="automode-row"):
                yield Select(
                    [("English", "en"), ("Español", "es")],
                    id="cfg-language",
                    allow_blank=False,
                )
            yield Static(t("cfg.references"), classes="section-title")
            yield Static(t("refs.warning"))
            yield ReferencesForm(id="cfg-refs")
            yield Static(t("cfg.triggers"), classes="section-title")
            yield Static(t("trig.help"))
            yield TriggersForm(id="cfg-triggers")
            with Horizontal(id="config-buttons"):
                yield Button(t("common.save"), variant="primary", id="cfg-save")
                yield Button(t("common.back"), id="cfg-back")
        yield Footer()

    def on_mount(self) -> None:
        self._config = config_module.load()
        self._loading = False
        form = self.query_one(RolesForm)
        for role, _ in ROLES:
            role_cfg = self._config.role(role)
            form.set_role(role, role_cfg.cli, role_cfg.model, role_cfg.effort)
        auto = self._config.automode
        self.query_one("#am-enabled", Checkbox).value = auto.enabled
        self.query_one("#am-branch", Checkbox).value = auto.create_branch
        self.query_one("#am-confirm-plan", Checkbox).value = auto.confirm_plan
        self.query_one("#upd-enabled", Checkbox).value = self._config.auto_update
        self.query_one("#am-max-iter", Input).value = str(auto.max_iterations)
        self.query_one("#am-tests", Input).value = auto.test_command
        self.query_one("#cfg-final-prompt", TextArea).text = self._config.final_prompt
        self.query_one("#hook-command", Input).value = self._config.hook.command
        for stage in parse_stages(self._config.hook.stages):
            self.query_one(f"#hook-stage-{stage}", Checkbox).value = True
        names = editor_module.available_editors()
        select = self.query_one("#editor-name", Select)
        select.set_options([(name, name) for name in names])
        self.query_one("#editor-enabled", Checkbox).value = self._config.editor.enabled
        if self._config.editor.editor in names:
            select.value = self._config.editor.editor
        self.query_one("#editor-mode", Select).value = (
            self._config.editor.mode if self._config.editor.mode in ("window", "split", "none") else "window"
        )
        self.query_one("#editor-side", Select).value = (
            self._config.editor.side if self._config.editor.side in ("left", "right") else "left"
        )
        self.query_one("#cfg-language", Select).value = (
            self._config.language if self._config.language in LANGUAGES else "en"
        )
        self.query_one("#cfg-refs", ReferencesForm).set_references(
            references_module.load_global()
        )
        self.query_one("#cfg-triggers", TriggersForm).set_triggers(
            triggers_module.load_global()
        )
        self._load_models()

    # ------------------------------------------------------------------ #
    # Model loading (async worker: cancelable with Esc)
    # ------------------------------------------------------------------ #
    def _load_models(self) -> None:
        self._loading = True
        self._models_worker = self.run_worker(
            self._fetch_models,
            exclusive=True,
            group="models",
            exit_on_error=False,
        )

    async def _fetch_models(self) -> None:
        models_map = await fetch_all_models(KNOWN_CLIS)
        variants_map = await fetch_all_variants(KNOWN_CLIS)
        self._apply_models(models_map, variants_map)

    def _apply_models(
        self,
        models_map: dict[str, list[str]],
        variants_map: dict[str, dict[str, list[str]]] | None = None,
    ) -> None:
        self._loading = False
        form = self.query_one(RolesForm)
        form.set_models(models_map)
        if variants_map is not None:
            form.set_variants(variants_map)
        summary = " · ".join(
            t("cfg.models.count", cli=cli, count=len(models)) if models else t("cfg.models.unavailable", cli=cli)
            for cli, models in models_map.items()
        )
        self.query_one("#models-status", Static).update(summary)

    def _cancel_loading(self) -> bool:
        """Cancel model loading if still in progress. True if cancelled."""
        if self._loading:
            self._models_worker.cancel()
            self._loading = False
            self.query_one("#models-status", Static).update(
                t("cfg.models.canceled")
            )
            return True
        return False

    # ------------------------------------------------------------------ #
    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-back":
            self._cancel_loading()
            self.dismiss()
            return
        self._save()

    def action_back(self) -> None:
        # With a load in progress, the first Esc cancels it; the second exits.
        if self._cancel_loading():
            return
        self.dismiss()

    def _save(self) -> None:
        try:
            max_iter = int(self.query_one("#am-max-iter", Input).value or "5")
        except ValueError:
            self.notify(t("cfg.error.max_iter_int"), severity="error")
            return
        if max_iter < 1:
            self.notify(t("cfg.error.max_iter_min"), severity="error")
            return

        previous_language = self._config.language
        cfg = Config()
        form = self.query_one(RolesForm)
        for role, _ in ROLES:
            role_cfg = cfg.role(role)
            role_cfg.cli, role_cfg.model, role_cfg.effort = form.role_values(role)
        cfg.automode.enabled = self.query_one("#am-enabled", Checkbox).value
        cfg.automode.create_branch = self.query_one("#am-branch", Checkbox).value
        cfg.automode.confirm_plan = self.query_one("#am-confirm-plan", Checkbox).value
        cfg.auto_update = self.query_one("#upd-enabled", Checkbox).value
        cfg.automode.max_iterations = max_iter
        cfg.automode.test_command = self.query_one("#am-tests", Input).value.strip()
        cfg.final_prompt = self.query_one("#cfg-final-prompt", TextArea).text.strip()
        cfg.hook.command = self.query_one("#hook-command", Input).value.strip()
        cfg.hook.stages = format_stages([
            stage for stage in HOOK_STAGES
            if self.query_one(f"#hook-stage-{stage}", Checkbox).value
        ])
        cfg.editor.enabled = self.query_one("#editor-enabled", Checkbox).value
        selected = self.query_one("#editor-name", Select).value
        cfg.editor.editor = "" if selected is Select.BLANK else str(selected)
        cfg.editor.mode = str(self.query_one("#editor-mode", Select).value)
        cfg.editor.side = str(self.query_one("#editor-side", Select).value)
        cfg.language = str(self.query_one("#cfg-language", Select).value)
        config_module.save(cfg)
        references_module.save_global(
            self.query_one("#cfg-refs", ReferencesForm).references()
        )
        triggers_module.save_global(
            self.query_one("#cfg-triggers", TriggersForm).triggers()
        )
        set_language(cfg.language)
        self.notify(t("cfg.saved"))
        if cfg.language != previous_language:
            self.notify(t("cfg.language_notice"), severity="warning")
        self.dismiss()
