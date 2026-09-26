"""A scoped follower's trailing figures count only its own session's runs.

The defect this pins is one keyword argument at the command's own call site:
``reckon crew follow --session S`` scopes the rows it delivers but left the
trailing counters describing the whole project's fleet, so a reader with one
worker in flight read six. The capability to re-scope the figures already
existed on ``format_watch_transition``; nothing passed the session to it, and
every assertion about the helper passed while the surface stayed wrong. So this
file drives the command, not the helper: it feeds the command one project-wide
transition over a fixture holding two sessions whose populations differ, and
reads the line the command actually prints.

Three facts are asserted over that one fixture, because the change has two
declared halves:

* the scoped follower reads only its own session — 1 working, 0 blocked;
* the unscoped follower over the same fixture is unchanged — 2 working,
  1 blocked — which protects every peer coordinator reading an unscoped pane;
* the two differ, so a renderer that ignored ``--session`` could not pass by
  agreement. The blocked run belongs to the foreign session precisely because
  the observed defect was a blocked figure the reader could do nothing about.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew
from reckon.crew import runs

# The counter block the ticker prints at the end of each transition line:
# ``<n>w <n>b <n>u <n>q``, each count followed by its bucket's letter with
# whitespace between the cells, so a two-digit count is its own token rather
# than running into the bucket beside it. The pattern requires that whitespace,
# so a block that abutted two-digit counts would not match it at all. It is
# matched whole rather than as a prefix, so a bucket added later cannot slip
# past the prefix of an older count.
_COUNTERS = re.compile(r"(\d+)w\s+(\d+)b\s+(\d+)u\s+(\d+)q")

_POINTER_RUN_IDS = ("r-own-work", "r-peer-work", "r-peer-block")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep pointers, manifests and registrations in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _write_pointer(
    home: Path,
    run_id: str,
    node: str,
    *,
    session: str,
    phase: str,
    manifest_status: str | None = None,
) -> None:
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    pointer = {
        "run_id": run_id,
        "project": "proj",
        "session": session,
        "node": {"id": node, "plan": "plan-a", "time_budget": "20m"},
        "phase": phase,
        "created_at": runs._utc_now(),
        "log_path": str(log),
    }
    if manifest_status is not None:
        manifest = home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {node}\nstatus: {manifest_status}\nblockers: none\n"
        )
        pointer["manifest_path"] = str(manifest)
    crew._write_json(crew.pointer_path(run_id), pointer)


# The one transition the producer delivers: stamped with the owning session,
# carrying the whole project's totals the way the project-global watch stream
# does. Its figures describe the fleet, so a renderer that ignores the session
# prints them unchanged.
_FLEET_TRANSITION = {
    "event": "transition",
    "run_id": "r-own-work",
    "node": "own-node",
    "session": "mine",
    "from_state": "discover",
    "to_state": "working",
    "working": 2,
    "blocked": 1,
    "unpromoted": 0,
}


def _stub_producer(monkeypatch) -> None:
    """Feed the command one project-wide transition, then let it return."""

    def fake_lines(project, **kwargs):
        yield {**_FLEET_TRANSITION, "project": project}

    monkeypatch.setattr(cli, "_follow_watch_lines", fake_lines)


def _follow(*args: str) -> str:
    result = CliRunner().invoke(
        cli.main,
        ["crew", "follow", "--project", "proj", "--width", "160", "--no-color", *args],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _counters(output: str) -> tuple[int, int, int, int]:
    match = _COUNTERS.search(output)
    assert match is not None, f"no trailing counter block in: {output!r}"
    return tuple(int(group) for group in match.groups())


@pytest.fixture()
def two_sessions(home):
    """One working run of ``mine``; one working and one blocked of ``peers``."""
    _write_pointer(home, "r-own-work", "own-node", session="mine", phase="working")
    _write_pointer(home, "r-peer-work", "peer-node", session="peers", phase="working")
    _write_pointer(
        home,
        "r-peer-block",
        "block-node",
        session="peers",
        phase="working",
        manifest_status="blocked",
    )
    return home


def test_a_scoped_follower_counts_only_its_own_session(
    two_sessions, monkeypatch
) -> None:
    _stub_producer(monkeypatch)
    output = _follow("--session", "mine")
    assert _counters(output) == (1, 0, 0, 0)


def test_an_unscoped_follower_over_the_same_fixture_is_unchanged(
    two_sessions, monkeypatch
) -> None:
    _stub_producer(monkeypatch)
    output = _follow()
    assert _counters(output) == (2, 1, 0, 0)


@pytest.fixture()
def two_digit_session(home):
    """One session whose own figures need two digits in two buckets."""
    for index in range(10):
        _write_pointer(
            home, f"r-own-work-{index}", f"own-{index}", session="mine", phase="working"
        )
    for index in range(12):
        _write_pointer(
            home,
            f"r-own-block-{index}",
            f"block-{index}",
            session="mine",
            phase="working",
            manifest_status="blocked",
        )
    return home


def test_two_digit_counts_reach_the_line_as_separate_tokens(
    two_digit_session, monkeypatch
) -> None:
    """A two-digit count does not absorb the bucket beside it.

    The scoped figures are recomputed from the pointers, so ten working runs and
    twelve blocked runs print ``10w 12b 0u 0q``: the reader sees two tokens, not
    the ``10w12b`` the abutting block rendered. The pattern requires whitespace
    between a count and the next bucket's letter, so the abutting form leaves the
    line with no counter block this reader can find at all.
    """
    _stub_producer(monkeypatch)
    output = _follow("--session", "mine")
    assert _counters(output) == (10, 12, 0, 0)
    assert "10w 12b 0u 0q" in output
    assert "10w12b" not in output


def test_the_scoped_figures_differ_from_the_unscoped_figures(
    two_sessions, monkeypatch
) -> None:
    """Guards against a fixture where both readings agree by accident.

    Were the two populations equal, a renderer that ignored ``--session`` would
    pass both assertions above. Requiring them to differ is what makes the
    scoped assertion a statement about scoping rather than about the fixture.
    """
    _stub_producer(monkeypatch)
    scoped = _counters(_follow("--session", "mine"))
    unscoped = _counters(_follow())
    assert scoped != unscoped
    assert scoped == (1, 0, 0, 0)
    assert unscoped == (2, 1, 0, 0)


def _real_home_pointer_paths() -> list[Path]:
    """Where this test's pointers would land without the ``RECKON_HOME`` override.

    Resolving the same paths with the override popped yields the real live
    directory's location; the pointer names are unique to this test, so a live
    session writing its own runs elsewhere in the shared home cannot race the
    check.
    """
    saved = os.environ.pop("RECKON_HOME", None)
    try:
        return [crew.pointer_path(run_id) for run_id in _POINTER_RUN_IDS]
    finally:
        if saved is not None:
            os.environ["RECKON_HOME"] = saved


def test_the_real_live_pointer_directory_is_untouched(
    two_sessions, monkeypatch
) -> None:
    """Every write lands in the temporary home, never the real one.

    An isolated read does not prove an isolated write, so the real live pointer
    directory is checked directly, after the command has run: the pointers this
    test authors must be absent there.
    """
    real_pointers = _real_home_pointer_paths()
    for pointer in real_pointers:
        assert not pointer.exists(), f"a real-home pointer pre-exists: {pointer}"

    _stub_producer(monkeypatch)
    _follow("--session", "mine")
    _follow()

    for pointer in real_pointers:
        assert not pointer.exists(), f"the real home gained a pointer: {pointer}"
