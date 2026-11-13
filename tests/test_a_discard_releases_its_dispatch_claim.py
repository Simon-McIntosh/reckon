"""A discard gives up the node dispatch claim its run holds.

A dispatch takes the exclusive claim over a node's worktree path before it cuts
the tree, and releases it only when the launch is decided. A dispatch cut off
between the two — a service re-executing under the same pid ends its thread —
leaves the claim behind, and while its holder process lives no second dispatch
of that node can take it. A deliberate discard of that run is the coordinator
saying the attempt is over, so the claim goes with it. A claim naming a
different run guards a dispatch nobody discarded, and stays.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew import dispatch_claims as claims
from reckon.crew import promotion
from reckon.crew.node import CrewError

PROJECT = "discard-claim-fixture"
WORKTREE = "discard-claim-worktree"
NODE = "discard-claim-node"


@pytest.fixture()
def crew_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised crew home; the fixture never touches the operator's."""
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home / "crew" / "claims"


def _take_claim(run_id: str) -> claims._NodeDispatchClaim:
    return claims._claim_node_dispatch(
        project=PROJECT,
        worktree_identity=WORKTREE,
        node_id=NODE,
        run_id=run_id,
        session="discard-claim-session",
    )


def _store_pointer(run_id: str) -> None:
    """A stopped pointer whose own dispatch took a claim over this node."""
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": "discard-claim-session",
        "phase": "starting",
        "node": {"id": NODE},
        "created_at": "2026-11-07T00:00:00+00:00",
    }
    crew._write_json(crew.pointer_path(run_id), record)


def _record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_discard_releases_the_claim_its_run_holds(crew_store: Path) -> None:
    """After a discard, a dispatch of the same node takes the claim again."""
    held = _take_claim("abandoned-run")
    _store_pointer("abandoned-run")

    result = promotion.discard("abandoned-run")

    assert not held.path.exists()
    # A deliberate discard is not a stale claim: the claim is given up, not
    # displaced to the artifact the reclaim path leaves for a stale one.
    assert list(held.path.parent.glob(f"{held.path.name}.reclaimed-*")) == []
    second = _take_claim("next-run")
    assert second.reclaimed is None
    assert _record(second.path)["run_id"] == "next-run"
    assert result.get("dispatch_claim_released") == [str(held.path)]
    second.release()


def test_discard_leaves_a_claim_naming_another_run(crew_store: Path) -> None:
    """A claim another run holds over the same node survives the discard."""
    other = _take_claim("other-run")
    _store_pointer("my-run")

    promotion.discard("my-run")

    assert other.path.exists()
    assert _record(other.path)["run_id"] == "other-run"
    with pytest.raises(CrewError, match="other-run"):
        _take_claim("next-run")
    assert _record(other.path)["run_id"] == "other-run"
    other.release()
