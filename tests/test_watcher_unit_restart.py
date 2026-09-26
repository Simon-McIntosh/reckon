"""The restart policy of every systemd unit this repository renders.

The unit text is written in more than one place, so a policy repair applied to
one copy leaves the other rendering a unit that never comes back after a clean
exit. These cases read the rendered text from the generators rather than from
the source that writes it, and the enumeration case fails when a template is
added elsewhere so a third copy cannot pass unnoticed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reckon import service
from reckon.crew import paid_lanes
from reckon.crew import runs as crew_runs

REPO_ROOT = Path(__file__).resolve().parent.parent

# The modules that render systemd unit text. Every entry is rendered by
# test_a_generator_renders_restart_always_with_a_brief_delay, and the tree scan
# requires the set to equal what the source carries, so a generator added
# elsewhere fails both halves until it is listed here and rendered.
RESTART_POLICY_UNITS = ("reckon/crew/runs.py", "reckon/service.py")

# A timer-activated oneshot unit runs once per activation and exits, and
# systemd refuses to apply a Restart= policy to Type=oneshot. The refresh
# publisher is one, so it is inventoried here with its own assertion rather
# than folded into the watcher restart policy it cannot carry.
ONESHOT_UNITS = ("reckon/crew/paid_lanes.py",)

UNIT_GENERATORS = RESTART_POLICY_UNITS + ONESHOT_UNITS

# An exited unit is worth a brief pause before the manager brings it back, but
# not long enough that an exited process stays down for a meaningful fraction
# of a minute.
MAX_RESTART_DELAY_SECONDS = 30


def _rendered_units(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Render every generator's unit text, with the config home redirected.

    RECKON_HOME is pointed at the temporary directory so a render resolves its
    log path there; the case that reads the resolved path back proves the
    redirect took effect, which turns "the real account home was not touched"
    into a fact rather than a hopeful reading.
    """
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    binary = tmp_path / "bin" / "reckon"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n")
    return {
        "reckon/crew/runs.py": crew_runs.render_watch_unit(
            "demo", environment={"PATH": "/usr/bin:/bin"}, executable=str(binary)
        ),
        "reckon/service.py": service.render_unit(executable=binary, node=binary),
        "reckon/crew/paid_lanes.py": paid_lanes.render_service_unit(
            executable=str(binary), root=tmp_path
        ),
    }


def _unit_template_sources() -> set[str]:
    """Repo-relative modules whose source carries a systemd unit template."""
    found: set[str] = set()
    for path in (REPO_ROOT / "reckon").rglob("*.py"):
        if re.search(r"^\s*\[Service\]\s*$", path.read_text(), re.MULTILINE):
            found.add(str(path.relative_to(REPO_ROOT)))
    return found


@pytest.mark.parametrize("generator", RESTART_POLICY_UNITS)
def test_a_generator_renders_restart_always_with_a_brief_delay(
    generator: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    unit = _rendered_units(tmp_path, monkeypatch)[generator]
    assert "Restart=always" in unit
    delay = re.search(r"^RestartSec=(\d+)$", unit, re.MULTILINE)
    assert delay is not None, unit
    assert int(delay.group(1)) <= MAX_RESTART_DELAY_SECONDS


@pytest.mark.parametrize("generator", RESTART_POLICY_UNITS)
def test_no_generator_still_restarts_only_on_failure(
    generator: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    unit = _rendered_units(tmp_path, monkeypatch)[generator]
    assert "Restart=on-failure" not in unit, unit


@pytest.mark.parametrize("generator", ONESHOT_UNITS)
def test_a_timer_activated_oneshot_unit_carries_no_restart_policy(
    generator: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A oneshot unit runs once per activation, so systemd refuses it a Restart.

    Asserting the Type is what keeps the exemption honest: a template that
    quietly dropped the oneshot type would need the restart policy above
    rather than this acknowledgement.
    """
    unit = _rendered_units(tmp_path, monkeypatch)[generator]
    assert "Type=oneshot" in unit, unit
    assert "Restart=" not in unit, unit


def test_every_unit_template_in_the_tree_is_covered_here():
    assert _unit_template_sources() == set(UNIT_GENERATORS)


def test_the_config_home_redirect_reaches_the_rendered_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    unit = _rendered_units(tmp_path, monkeypatch)["reckon/crew/runs.py"]
    assert str(tmp_path / "config") in unit
