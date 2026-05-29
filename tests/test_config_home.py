"""Tests for the config-home resolution order in reckon._store.

Resolution order shipped (committed-but-not-loaded — no directory migration):
    RECKON_HOME env override
        → ~/.config/reckon (if it exists)
        → ~/docs-server (legacy fallback)

The env overrides RECKON_MOUNTS_PATH / RECKON_STATE_ROOT must STILL win over
_config_home — existing tests and installs depend on that.

Hermeticity: every test monkeypatches Path.home() to a tmp dir and clears ALL
three env vars (RECKON_HOME, RECKON_MOUNTS_PATH, RECKON_STATE_ROOT) so a value
leaking in from the real shell cannot taint an assertion. Nothing is created
under the real home.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import reckon._store as store


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a tmp dir and clear all reckon env overrides."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("RECKON_HOME", "RECKON_MOUNTS_PATH", "RECKON_STATE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    return home


# ── env overrides still win ──────────────────────────────────────────────────


def test_mounts_path_env_override_wins(fake_home, tmp_path, monkeypatch):
    """RECKON_MOUNTS_PATH must override _config_home even when ~/.config/reckon exists."""
    (fake_home / ".config" / "reckon").mkdir(parents=True)
    explicit = tmp_path / "custom-mounts.json"
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(explicit))
    assert store._mounts_path() == explicit.expanduser().resolve()


def test_state_root_env_override_wins(fake_home, tmp_path, monkeypatch):
    """RECKON_STATE_ROOT must override _config_home even when ~/.config/reckon exists."""
    (fake_home / ".config" / "reckon").mkdir(parents=True)
    explicit = tmp_path / "custom-state"
    monkeypatch.setenv("RECKON_STATE_ROOT", str(explicit))
    assert store._state_root() == explicit.expanduser().resolve()


def test_config_home_env_override_wins(fake_home, tmp_path, monkeypatch):
    """RECKON_HOME wins over both ~/.config/reckon and ~/docs-server."""
    (fake_home / ".config" / "reckon").mkdir(parents=True)
    (fake_home / "docs-server").mkdir()
    explicit = tmp_path / "elsewhere"
    monkeypatch.setenv("RECKON_HOME", str(explicit))
    assert store._config_home() == explicit.expanduser().resolve()
    assert store._mounts_path() == explicit.expanduser().resolve() / "mounts.json"
    assert store._state_root() == explicit.expanduser().resolve() / "state"


# ── ~/.config/reckon preferred when it exists ────────────────────────────────


def test_resolves_to_xdg_when_present(fake_home):
    """With ~/.config/reckon present, _config_home resolves there."""
    xdg = fake_home / ".config" / "reckon"
    xdg.mkdir(parents=True)
    assert store._config_home() == xdg
    assert store._mounts_path() == xdg / "mounts.json"
    assert store._state_root() == xdg / "state"


# ── legacy fallback when only ~/docs-server present ──────────────────────────


def test_falls_back_to_legacy_docs_server(fake_home):
    """With only ~/docs-server present (no ~/.config/reckon), falls back there."""
    legacy = fake_home / "docs-server"
    legacy.mkdir()
    assert store._config_home() == legacy
    assert store._mounts_path() == legacy / "mounts.json"
    assert store._state_root() == legacy / "state"


# ── precedence: ~/.config/reckon preferred over ~/docs-server when both exist ─


def test_xdg_preferred_over_legacy_when_both_exist(fake_home):
    xdg = fake_home / ".config" / "reckon"
    xdg.mkdir(parents=True)
    (fake_home / "docs-server").mkdir()
    assert store._config_home() == xdg
    assert store._mounts_path() == xdg / "mounts.json"


def test_falls_back_to_legacy_path_when_neither_exists(fake_home):
    """Neither dir present → still returns the legacy ~/docs-server path so the
    orchestrator's later migration is what flips resolution, not a crash here."""
    assert store._config_home() == fake_home / "docs-server"
