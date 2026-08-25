"""Tests de configuración global."""

from __future__ import annotations

from grafeno import config, paths
from grafeno.config import Config, RoleConfig


def test_load_creates_defaults():
    cfg = config.load()
    assert cfg.planner.cli == "opencode"
    assert cfg.implementer.cli == "kimi"
    assert cfg.reviewer.cli == "opencode"
    assert cfg.final.cli == "opencode"
    assert cfg.final.model == ""
    assert cfg.automode.max_iterations == 5
    assert paths.config_path().exists()


def test_roundtrip():
    cfg = Config()
    cfg.planner.model = "opencode-go/kimi-k3"
    cfg.implementer.cli = "opencode"
    cfg.implementer.model = "opencode-go/glm-5.3"
    cfg.reviewer.model = 'modelo con "comillas" y\\barra'
    cfg.final.cli = "kimi"
    cfg.final.model = "kimi-code/k3"
    cfg.automode.enabled = True
    cfg.automode.max_iterations = 3
    cfg.automode.test_command = "pytest -q"
    cfg.automode.create_branch = False
    cfg.automode.confirm_plan = True
    config.save(cfg)

    loaded = config.load()
    assert loaded.planner.model == "opencode-go/kimi-k3"
    assert loaded.implementer.cli == "opencode"
    assert loaded.implementer.model == "opencode-go/glm-5.3"
    assert loaded.reviewer.model == 'modelo con "comillas" y\\barra'
    assert loaded.final.cli == "kimi"
    assert loaded.final.model == "kimi-code/k3"
    assert loaded.automode.enabled is True
    assert loaded.automode.max_iterations == 3
    assert loaded.automode.test_command == "pytest -q"
    assert loaded.automode.create_branch is False
    assert loaded.automode.confirm_plan is True


def test_theme_roundtrip():
    """La paleta elegida sobrevive al roundtrip to_dict/from_dict."""
    cfg = Config()
    cfg.theme = "nord"
    config.save(cfg)

    loaded = config.load()
    assert loaded.theme == "nord"


def test_theme_defaults_to_empty():
    """Sin clave theme en disco, la config carga con tema por defecto."""
    loaded = Config.from_dict({})
    assert loaded.theme == ""


def test_final_prompt_roundtrip():
    cfg = Config()
    cfg.final_prompt = "Revisa el CHANGELOG\ny actualiza README"
    config.save(cfg)

    loaded = config.load()
    assert loaded.final_prompt == "Revisa el CHANGELOG\ny actualiza README"


def test_hook_roundtrip():
    cfg = Config()
    cfg.hook.command = "make notify"
    cfg.hook.stages = "plan,final"
    config.save(cfg)

    loaded = config.load()
    assert loaded.hook.command == "make notify"
    assert loaded.hook.stages == "plan,final"


def test_editor_defaults():
    cfg = config.load()
    assert cfg.editor.enabled is False
    assert cfg.editor.editor == ""
    assert cfg.editor.mode == "window"
    assert cfg.editor.side == "left"


def test_editor_roundtrip():
    cfg = Config()
    cfg.editor.enabled = True
    cfg.editor.editor = "zed"
    cfg.editor.mode = "split"
    cfg.editor.side = "right"
    config.save(cfg)

    loaded = config.load()
    assert loaded.editor.enabled is True
    assert loaded.editor.editor == "zed"
    assert loaded.editor.mode == "split"
    assert loaded.editor.side == "right"


def test_resolve_editor_config_project_override(tmp_path):
    cfg = Config()
    cfg.editor.editor = "code"
    project_toml = tmp_path / ".grafeno.toml"
    project_toml.write_text(
        '[editor]\neditor = "tode"\nmode = "split"\n',
        encoding="utf-8",
    )

    resolved = config.resolve_editor_config(cfg, tmp_path)
    assert resolved.editor == "tode"
    assert resolved.mode == "split"
    assert resolved.side == "left"  # heredado, no sobreescrito


def test_resolve_editor_config_without_project_file(tmp_path):
    cfg = Config()
    cfg.editor.editor = "code"
    assert not (tmp_path / ".grafeno.toml").exists()

    resolved = config.resolve_editor_config(cfg, tmp_path)
    assert resolved.editor == "code"


def test_resolve_editor_config_invalid_toml(tmp_path):
    cfg = Config()
    cfg.editor.editor = "code"
    (tmp_path / ".grafeno.toml").write_text("= = =", encoding="utf-8")

    resolved = config.resolve_editor_config(cfg, tmp_path)
    assert resolved.editor == "code"  # sin sobreescritura, no se propaga la excepción


def test_resolve_editor_config_none_workdir():
    cfg = Config()
    cfg.editor.editor = "code"
    assert config.resolve_editor_config(cfg, None) is cfg.editor


def test_role_config_effort_roundtrip():
    """RoleConfig serializa y recupera ``effort`` correctamente."""
    role = RoleConfig("opencode", "p/m", "high")
    assert role.to_dict() == {"cli": "opencode", "model": "p/m", "effort": "high"}
    loaded = RoleConfig.from_dict(role.to_dict(), default_cli="opencode")
    assert loaded.effort == "high"


def test_role_config_effort_defaults_to_empty():
    """``from_dict`` sin la clave ``effort`` carga con valor vacío."""
    loaded = RoleConfig.from_dict({"cli": "opencode", "model": "m"}, default_cli="opencode")
    assert loaded.effort == ""


def test_config_effort_roundtrip():
    """Config persiste y recupera ``effort`` por rol vía ``save``/``load``."""
    cfg = Config()
    cfg.planner.effort = "high"
    cfg.implementer.effort = "max"
    cfg.reviewer.effort = ""
    cfg.final.effort = "low"
    config.save(cfg)

    loaded = config.load()
    assert loaded.planner.effort == "high"
    assert loaded.implementer.effort == "max"
    assert loaded.reviewer.effort == ""
    assert loaded.final.effort == "low"


def test_auto_update_default_false():
    assert config.Config().auto_update is False
    assert config.load().auto_update is False


def test_auto_update_roundtrip():
    cfg = config.Config()
    cfg.auto_update = True
    config.save(cfg)
    assert config.load().auto_update is True
