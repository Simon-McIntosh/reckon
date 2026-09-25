"""Where ``reckon crew path`` says reckon keeps its files.

The verb exists so consumers stop hardcoding one machine's configuration home,
which makes the resolution order it prints through the whole of its contract.
Every kind is therefore asserted under all three branches ``_config_home``
resolves by — an explicit ``RECKON_HOME``, an existing ``~/.config/reckon``,
and the legacy ``~/docs-server`` — and every selector rule is exercised in both
directions: the combinations that resolve, and the combinations a kind refuses
rather than silently ignoring.

Hermeticity: each test points ``Path.home()`` at a temporary tree and clears
the reckon environment overrides, so no assertion here reads or writes the
real home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main

# One run id for every run-keyed case. The verb prints a location and never
# requires the run to exist, so the value only has to be a name a consumer
# would pass on the command line.
RUN = "r-20260924T000000000000-example-node"

# kind, project, run, path relative to the resolved configuration home
RESOLVED = (
    ("config-home", None, None, ()),
    ("reports", None, None, ("crew", "reports")),
    ("reports", "nova", None, ("crew", "reports", "nova")),
    ("runs", None, None, ("crew", "runs")),
    ("runs", None, RUN, ("crew", "runs", RUN)),
    ("live", None, None, ("crew", "live")),
    ("live", None, RUN, ("crew", "live", f"{RUN}.json")),
    ("reviews", "nova", None, ("crew", "reviews", "nova")),
    ("reviews", "nova", RUN, ("crew", "reviews", "nova", f"{RUN}.json")),
)

# kind, project, run, the refusal that names the accepted selector
REFUSED = (
    ("config-home", "nova", None, "takes no selector"),
    ("config-home", None, RUN, "takes no selector"),
    ("reports", None, RUN, "--project only"),
    ("reports", "nova", RUN, "--project only"),
    ("runs", "nova", None, "--run only"),
    ("live", "nova", RUN, "--run only"),
    ("reviews", None, None, "requires --project"),
    ("reviews", None, RUN, "requires --project"),
)


@pytest.fixture()
def fake_home(tmp_path, monkeypatch):
    """Point ``Path.home()`` at a temporary tree and clear env overrides."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for var in ("RECKON_HOME", "RECKON_MOUNTS_PATH", "RECKON_STATE_ROOT"):
        monkeypatch.delenv(var, raising=False)
    return home


def _args(kind, project, run_id):
    args = ["crew", "path", "--kind", kind]
    if project is not None:
        args += ["--project", project]
    if run_id is not None:
        args += ["--run", run_id]
    return args


def _output(result):
    """Both streams, because a refusal is written to stderr and a path to stdout."""
    stderr = getattr(result, "stderr", "") or ""
    return result.output + stderr


def _assert_branch(home):
    """Every kind and selector form resolves under ``home``."""
    for kind, project, run_id, relative in RESOLVED:
        result = CliRunner().invoke(main, _args(kind, project, run_id))
        where = (kind, project, run_id)
        assert result.exit_code == 0, (where, _output(result))
        printed = Path(result.output.strip())
        assert printed == home.joinpath(*relative), where
        assert printed.is_absolute(), where
        # One newline-terminated path, never a JSON envelope to unwrap.
        assert result.output.endswith("\n"), where
        assert result.output.count("\n") == 1, where
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.output)


def test_reckon_home_wins_over_both_candidates(fake_home, tmp_path, monkeypatch):
    """The RECKON_HOME branch resolves every kind past both fallbacks."""
    (fake_home / ".config" / "reckon").mkdir(parents=True)
    (fake_home / "docs-server").mkdir()
    explicit = tmp_path / "explicit"
    monkeypatch.setenv("RECKON_HOME", str(explicit))
    _assert_branch(explicit.expanduser().resolve())


def test_existing_xdg_config_home_is_the_second_branch(fake_home):
    """With no override, an existing ~/.config/reckon takes every kind."""
    xdg = fake_home / ".config" / "reckon"
    xdg.mkdir(parents=True)
    _assert_branch(xdg)


def test_legacy_docs_server_is_the_last_branch(fake_home):
    """With only ~/docs-server present, every kind still resolves."""
    legacy = fake_home / "docs-server"
    legacy.mkdir()
    _assert_branch(legacy)


def test_a_selector_a_kind_does_not_take_is_refused(fake_home):
    """A dropped selector answers a different question, so it exits non-zero."""
    for kind, project, run_id, refusal in REFUSED:
        result = CliRunner().invoke(main, _args(kind, project, run_id))
        where = (kind, project, run_id)
        text = _output(result)
        assert result.exit_code != 0, (where, text)
        assert f"--kind {kind}" in text, (where, text)
        assert refusal in text, (where, text)


def test_an_unknown_kind_is_refused(fake_home):
    """The kind vocabulary is closed; a name outside it is not a path."""
    result = CliRunner().invoke(main, ["crew", "path", "--kind", "somewhere-else"])
    assert result.exit_code != 0
    assert "somewhere-else" in _output(result)
