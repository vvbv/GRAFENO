"""Tests of the processing-profiles module (storage + lookup)."""

from __future__ import annotations

from grafeno import paths, profiles
from grafeno.config import RoleConfig
from grafeno.profiles import PROFILE_ROLES, Profile


def _sample(name="calidad") -> Profile:
    profile = Profile(name=name)
    profile.planner = RoleConfig(cli="opencode", model="m1", effort="high")
    profile.implementer = RoleConfig(cli="kimi", model="k3")
    return profile


def _raise_oserror(*args, **kwargs):
    raise OSError("boom")


def test_profile_roundtrip_all_roles():
    profile = _sample()
    clone = Profile.from_dict(profile.to_dict())
    assert clone.name == profile.name
    for role in PROFILE_ROLES:
        assert clone.role(role).cli == profile.role(role).cli
        assert clone.role(role).model == profile.role(role).model
        assert clone.role(role).effort == profile.role(role).effort


def test_load_global_missing_file_returns_empty():
    assert profiles.load_global() == []


def test_save_and_load_global_roundtrip():
    profiles.save_global([_sample(), Profile(name="rapido")])
    loaded = profiles.load_global()
    assert [p.name for p in loaded] == ["calidad", "rapido"]
    assert loaded[0].implementer.cli == "kimi"


def test_corrupt_file_returns_empty():
    paths.profiles_path().write_text("not [valid toml", encoding="utf-8")
    assert profiles.load_global() == []


def test_find_by_name():
    saved = [_sample(), Profile(name="rapido")]
    assert profiles.find("rapido", saved).name == "rapido"
    assert profiles.find("inexistente", saved) is None
    assert profiles.find("", saved) is None


def test_summary_mentions_roles():
    text = _sample().summary()
    assert "plan=opencode/m1" in text
    assert "impl=kimi/k3" in text


def test_roles_summary_matches_profile_summary():
    profile = _sample()
    roles = {role: profile.role(role) for role in PROFILE_ROLES}
    assert profiles.roles_summary(roles) == profile.summary()
    assert "plan=opencode/m1" in profile.summary()


def test_roles_summary_defaults_for_missing_roles():
    assert profiles.roles_summary({}).count("opencode/default") == len(PROFILE_ROLES)


def test_save_global_writes_backup_on_overwrite():
    profiles.save_global([_sample("calidad")])
    first = paths.profiles_path().read_text(encoding="utf-8")
    profiles.save_global([_sample("rapido")])
    backup = paths.profiles_path().with_name("profiles.toml.bak")
    assert backup.read_text(encoding="utf-8") == first


def test_save_global_no_backup_on_first_write():
    profiles.save_global([_sample()])
    assert not paths.profiles_path().with_name("profiles.toml.bak").exists()


def test_save_global_backup_is_best_effort(monkeypatch):
    profiles.save_global([_sample("calidad")])
    monkeypatch.setattr("grafeno.profiles.shutil.copy2", _raise_oserror)
    profiles.save_global([_sample("rapido")])  # must not raise
    assert "rapido" in paths.profiles_path().read_text(encoding="utf-8")