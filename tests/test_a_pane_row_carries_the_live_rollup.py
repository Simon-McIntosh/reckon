"""A pane row names the session's own live nodes after its counters.

The counters at the end of every transition line say how much work is in
flight; a reader still cannot tell WHICH workers are in flight without reading
the whole stream, so the lead's *I should be able to see all work active* went
unmet on the latest line. This file pins the counter cell's new roll-up: the
names of the session's working nodes, drawn from the same session-scoped
population the count beside them is taken over.

Three facts are asserted over one fixture, driven through the command rather
than the helper, because the defect this shape exists for is a helper that is
correct while the surface never calls it:

* a session's own row names its three live nodes and omits a peer's run, with
  the working counter reading three;
* when the last live run settles, the row names no live node and the counter
  reads zero;
* the roll-up agrees with ``crew(view="live", session=...)`` over the same
  fixture, so the row and the live read cannot describe two different fleets.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, crew, crew_follow_commands
from reckon.crew import runs
from reckon.crew.query import project_live_rows

# The counter block the ticker prints: ``<n>w <n>h <n>d <n>W``, each count
# followed by its bucket's letter with whitespace between the cells. It is
# matched whole so the roll-up that follows it is the text past the match, and
# the working count is its first group.
_COUNTERS = re.compile(r"(\d+)w\s+(\d+)h\s+(\d+)d\s+(\d+)W")

_OWN_NODES = ("own-alpha", "own-beta", "own-gamma")
_PEER_NODE = "peer-node"


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
    alive: bool = True,
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
        "process_alive": alive,
    }
    if manifest_status is not None:
        manifest = home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {node}\nstatus: {manifest_status}\nblockers: none\n"
        )
        pointer["manifest_path"] = str(manifest)
    crew._write_json(crew.pointer_path(run_id), pointer)


# One project-wide transition, stamped with the owning session the way the
# watch stream stamps every event. Its figures describe the whole fleet, so a
# renderer that ignored the session would name the peer's node too.
_FLEET_TRANSITION = {
    "event": "transition",
    "run_id": "r-own-work-1",
    "node": _OWN_NODES[0],
    "session": "mine",
    "from_state": "discover",
    "to_state": "working",
    "working": 4,
    "blocked": 0,
    "unpromoted": 0,
}


def _stub_producer(monkeypatch) -> None:
    """Feed the command one project-wide transition, then let it return."""

    def fake_lines(project, **kwargs):
        yield {**_FLEET_TRANSITION, "project": project}

    monkeypatch.setattr(crew_follow_commands, "_follow_watch_lines", fake_lines)


def _follow(*args: str) -> str:
    result = CliRunner().invoke(
        cli.main,
        ["crew", "follow", "--project", "proj", "--width", "200", "--no-color", *args],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _counters_and_rollup(output: str) -> tuple[tuple[int, int, int, int], str]:
    """The counter block and the roll-up text that follows it on the row."""
    match = _COUNTERS.search(output)
    assert match is not None, f"no trailing counter block in: {output!r}"
    counts = tuple(int(group) for group in match.groups())
    return counts, output[match.end() :]


@pytest.fixture()
def three_own_one_peer(home):
    """Three working runs of ``mine`` and one working run of ``peer``."""
    for index, node in enumerate(_OWN_NODES, start=1):
        _write_pointer(
            home, f"r-own-work-{index}", node, session="mine", phase="working"
        )
    _write_pointer(home, "r-peer-work", _PEER_NODE, session="peer", phase="working")
    return home


def test_a_row_names_its_three_live_nodes_and_omits_the_peer(
    three_own_one_peer, monkeypatch
) -> None:
    _stub_producer(monkeypatch)
    counts, rollup = _counters_and_rollup(_follow("--session", "mine"))
    assert counts[0] == 3
    for node in _OWN_NODES:
        assert node in rollup, f"{node!r} missing from the roll-up: {rollup!r}"
    assert _PEER_NODE not in rollup, f"a peer's node reached the roll-up: {rollup!r}"


def test_a_settled_fleet_names_no_live_node(home, monkeypatch) -> None:
    """When the last live run settles its row reports none live.

    The same fixture with every run past its working phase: the row the settled
    run renders names no live node and the working counter reads zero, so the
    absence is the counter's own zero rather than a stale name.
    """
    for index, node in enumerate(_OWN_NODES, start=1):
        _write_pointer(
            home,
            f"r-own-work-{index}",
            node,
            session="mine",
            phase="complete",
            alive=False,
            manifest_status="complete",
        )
    _stub_producer(monkeypatch)
    counts, rollup = _counters_and_rollup(_follow("--session", "mine"))
    assert counts[0] == 0
    for node in _OWN_NODES:
        assert node not in rollup, f"a settled run is still named live: {rollup!r}"


def test_the_rollup_matches_the_live_view_for_the_session(
    three_own_one_peer, monkeypatch
) -> None:
    """The row and ``crew(view="live", session=S)`` read one population.

    Every run in the fixture is working, so the live view's own rows and the
    roll-up's live nodes are the same set; the assertion is that the row names
    exactly the nodes the live read returns for the session, so a second
    population cannot be derived for the pane.
    """
    records = runs._list_live_records(project="proj")
    live_rows = [
        row for row in project_live_rows(records, session="mine") if row.get("mine")
    ]
    live_nodes = {str(row.get("node") or "") for row in live_rows}

    _stub_producer(monkeypatch)
    counts, rollup = _counters_and_rollup(_follow("--session", "mine"))
    named = {node for node in _OWN_NODES if node in rollup}

    assert named == live_nodes
    assert counts[0] == len(live_nodes)
