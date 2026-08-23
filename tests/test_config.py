"""Tests de configuración global."""

from __future__ import annotations

from grafeno import config, paths
from grafeno.config import Config


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
