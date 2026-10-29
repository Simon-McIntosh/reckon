"""A run the coordinator has deferred leaves the obligations rows until its instant.

The rows name the runs the coordinator owes action on. A run they have put off
until an instant is not owed before that instant, so it is held out of the set
until the instant passes.

The obligations view honours the deferral, and these rows read that view's own
reader rather than a second copy of its rules: two readers of what needs the
coordinator that disagree leave the noisier one winning.

The instant is handed in rather than read, so a case sets the clock instead of
sleeping and the boundary is asserted either side of it.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from reckon import crew
from reckon.crew import needs_you as need_module
from reckon.crew import obligations as obligations_module
from reckon.crew import runs

PROJECT = "deferral-proj"
SESSION = "mine"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Keep live pointers, manifests and the review store in temporary state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _iso(epoch: float) -> str:
    """A UTC instant in the crew's own spelling."""
    moment = datetime.fromtimestamp(epoch, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_delivered_pointer(
    home, run_id: str, node: str, *, defer_until: float | None = None
) -> None:
    """Write one delivered run the way a dispatch leaves it behind.

    A manifest reading ``complete`` with its pointer still live is the
    unpromoted state the line names, so the run is in the set on its own merits
    and only the deferral can hold it out.
    """
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {node}\nstatus: complete\nblockers: none\n")
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": node},
        "phase": "working",
        "created_at": runs._utc_now(),
        "manifest_path": str(manifest),
    }
    if defer_until is not None:
        pointer["acknowledgement"] = {
            "reason": "the run's population is still at merge",
            "until": _iso(defer_until),
            "recorded_at": runs._utc_now(),
        }
    crew._write_json(crew.pointer_path(run_id), pointer)


def _nodes(moment: float) -> list[str]:
    return [
        str(row.get("node"))
        for row in need_module.needs_you_runs(PROJECT, session=SESSION, moment=moment)
    ]


def test_a_deferred_run_leaves_the_line_until_its_instant(home):
    """The three cases: held while deferred, named at the instant, expired named."""
    now = time.time()
    deferred_until = now + 3600.0
    _write_delivered_pointer(
        home, "r-deferred", "deferred-node", defer_until=deferred_until
    )
    _write_delivered_pointer(home, "r-expired", "expired-node", defer_until=now - 1.0)

    # An hour ahead: the deferred run is held out, and the run whose deferral
    # has already expired is named -- only what is still in force is held out.
    assert _nodes(now) == ["expired-node"], _nodes(now)

    # The instant is the boundary, asserted either side of it rather than by a
    # sleep: one second before, still parked; one second after, owed again.
    assert _nodes(deferred_until - 1.0) == ["expired-node"], _nodes(
        deferred_until - 1.0
    )
    returned = _nodes(deferred_until + 1.0)
    assert "deferred-node" in returned, returned
    assert "expired-node" in returned, returned

    # And the run is named once -- the derivation holds one row for it, not one
    # per reader of the deferral.
    rows = need_module.needs_you_runs(
        PROJECT, session=SESSION, moment=deferred_until + 1.0
    )
    assert [row["node"] for row in rows].count("deferred-node") == 1, rows


def test_the_line_reads_the_obligations_deferral_reader(home, monkeypatch):
    """The filter dispatches through the obligations view's own reader.

    A deferral visible only in that reader's answer, and nowhere in the pointer,
    must still hold the run out: that is what makes the two readers agree by
    construction rather than by two copies of one rule.
    """
    _write_delivered_pointer(home, "r-own", "own-node")
    assert _nodes(time.time()) == ["own-node"], _nodes(time.time())

    monkeypatch.setattr(
        obligations_module,
        "_acknowledgements_in_force",
        lambda project, *, now: {
            "r-own": {"reason": "parked", "until": _iso(time.time() + 3600.0)}
        },
    )

    assert need_module.needs_you_runs(PROJECT, session=SESSION) == []
