"""A queued node launches with every field it was dispatched with.

A queued request stores its node as data and rebuilds one at admission, so a
field the rebuild does not name is a field the launched worker never sees. A
composed figure review was queued and reached its worker carrying no rendered
picture for exactly this reason.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from reckon.crew import runs
from reckon.crew.node import TaskNode
from reckon.crew.queue_admission import _node_from_pointer

RUN_ID = "r-20261108T000000000000-queued-every-field"

# The two paths the rebuild rewrites rather than carries: both are re-derived
# from the launched run's own id when the queued run's directory reappears.
REWRITTEN_BY_REBUILD = ("manifest_path", "write_paths")


@pytest.fixture()
def queued_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew home at a temporary directory for this test."""
    home = tmp_path / "reckon-home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _dispatched(run_directory: Path) -> TaskNode:
    return TaskNode(
        id="node-queued",
        goal="judge the figure with its picture attached",
        plan="a-queue-holds-the-whole-plan",
        section="s36",
        after=["r-prior"],
        brief="the brief the run reads",
        brief_sha256="0" * 64,
        brief_path=str(run_directory / "brief.md"),
        role="review",
        spec_level="exact",
        done_when="the figure is judged against its picture",
        write_paths=["reckon/crew/node.py", str(run_directory / "scratch.py")],
        regenerate_paths=["docs/figures/generated.md"],
        anchor_paths=["docs/evidence/anchor.html"],
        time_budget="20m",
        over_fence_reason="the gate runs a long suite",
        manifest_path=str(run_directory / "manifest.md"),
        negative_control="naming the fields by hand again without images",
        estimated_hours=0.75,
        requires_decisions=["choice"],
        peer_scopes={"peer": ["docs/peer.html"]},
        images=["docs/figures/x/one.png"],
        inputs=["docs/figures/x/source.svg"],
        compares_with="docs/figures/x/other.png",
        admission_findings=[{"property": "scoped", "detail": "declared"}],
    )


def test_a_queued_node_keeps_every_field_it_was_dispatched_with(
    queued_home: Path,
) -> None:
    run_directory = runs.run_dir(RUN_ID)
    dispatched = _dispatched(run_directory)
    stored = dispatched.as_dict()

    assert set(stored) == {spec.name for spec in fields(TaskNode)}
    assert stored["images"] == ["docs/figures/x/one.png"]
    assert stored["peer_scopes"] == {"peer": ["docs/peer.html"]}
    assert stored["admission_findings"] == [
        {"property": "scoped", "detail": "declared"}
    ]

    rebuilt = _node_from_pointer({"run_id": RUN_ID, "node": stored})

    for spec in fields(TaskNode):
        if spec.name in REWRITTEN_BY_REBUILD:
            continue
        assert getattr(rebuilt, spec.name) == getattr(dispatched, spec.name), spec.name

    assert rebuilt.images == ["docs/figures/x/one.png"]
    assert rebuilt.inputs == ["docs/figures/x/source.svg"]
    assert rebuilt.compares_with == "docs/figures/x/other.png"


def test_run_directory_paths_are_dropped_and_foreign_ones_kept(
    queued_home: Path,
) -> None:
    run_directory = runs.run_dir(RUN_ID)
    inside_write = str(run_directory / "scratch.py")
    named_manifest = str(queued_home.parent / "named-manifest.md")
    stored = _dispatched(run_directory).as_dict()
    stored["manifest_path"] = named_manifest
    stored["write_paths"] = ["reckon/crew/node.py", inside_write]

    kept = _node_from_pointer({"run_id": RUN_ID, "node": stored})
    assert kept.manifest_path == named_manifest
    assert "reckon/crew/node.py" in kept.write_paths
    assert inside_write not in kept.write_paths

    dropped = _node_from_pointer(
        {"run_id": RUN_ID, "node": _dispatched(run_directory).as_dict()}
    )
    assert dropped.manifest_path == ""
    assert str(run_directory / "manifest.md") not in dropped.write_paths


def test_a_field_added_after_the_request_was_stored_takes_its_default(
    queued_home: Path,
) -> None:
    run_directory = runs.run_dir(RUN_ID)
    stored = _dispatched(run_directory).as_dict()
    # A request stored before these fields existed carries none of them.
    for name in ("images", "inputs", "compares_with", "admission_findings"):
        stored.pop(name)

    rebuilt = _node_from_pointer({"run_id": RUN_ID, "node": stored})

    assert rebuilt.images == []
    assert rebuilt.inputs == []
    assert rebuilt.compares_with == ""
    assert rebuilt.admission_findings == []


def test_the_real_crew_home_is_untouched(queued_home: Path) -> None:
    assert runs.crew_home().is_relative_to(queued_home)
    real_home = Path.home() / ".config" / "reckon" / "crew"
    assert not (real_home / "runs" / RUN_ID).exists()
    assert not (real_home / "live" / f"{RUN_ID}.json").exists()
