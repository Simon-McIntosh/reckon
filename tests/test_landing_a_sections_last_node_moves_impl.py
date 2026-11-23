"""Landing a section's last open node records the section done, so impl moves.

A plan's impl moves only when a section's declaration moves, and a coordinator
declares a section done after its last node lands. Without the landing writing
that declaration, the landing that should move impl is refused for not having
moved it, so a plan's final node is refused for the movement it is about to
make.

These tests drive a real promotion and landing against a temporary plan and
read the plan back: a lone node closes its section and moves impl; a node with
a sibling still open lands with the section left alone and no override; a
refused landing writes no declaration at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, _store, crew, ledger
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND
from tests.helpers.git import git as _git

PROJECT = "proj"
PLAN = "plan-a"
#: Two sections of one hour each: half the plan is done while s2 is open, and
#: the whole plan is done once s2 is recorded done.
SECTIONS = ("s1", "s2")


def _section_record(section: str, status: str) -> dict:
    return {
        "id": section,
        "effort_hours": 1.0,
        "capability": {
            "version": "1.0",
            "class": "general",
            "requirements": {
                "reasoning": "standard",
                "verification": "strict",
                "risk": "low",
            },
        },
        "attempts": 0,
        "status": status,
        "links": [],
    }


def _write_plan(path: Path, *, s2: str = "implementable") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    authored = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        '<title>Plan A</title></head><body><main class="plan-doc">'
        '<h2 id="s1">One section</h2><p>The first section body.</p>'
        '<h2 id="s2">Two section</h2><p>The second section body.</p>'
        "</main></body></html>"
    )
    path.write_text(
        _plan_html.write_state(
            authored,
            {
                "project": PROJECT,
                "type": "plan",
                "slug": PLAN,
                "title": "Plan A",
                "status": "active",
                "modified": "2026-11-23",
                "version": 0,
                "section_declarations": {"s1": "done", "s2": s2},
                "sections": [
                    _section_record("s1", "done"),
                    _section_record("s2", s2),
                ],
            },
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_plan(root / "docs" / "plans" / f"{PLAN}.html")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "worker@example.invalid")
    _git(root, "config", "user.name", "Worker")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _state(repository: Path) -> dict:
    state, _version = _store.read_plan(PROJECT, PLAN, repository, artifact_type="plan")
    return state


def _manifest(tmp_path: Path, run_id: str) -> Path:
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "commits: []\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    return manifest


def _pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    node_id: str = "node-a",
    section: str = "s2",
    phase: str | None = None,
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": "implement",
        "backend": "native",
        "created_at": "2026-11-23T06:00:00Z",
        "plan_impl_at_dispatch": 0.5,
        "manifest_path": str(_manifest(tmp_path, run_id)),
        "node": {
            "id": node_id,
            "plan": PLAN,
            "section": section,
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    if phase is not None:
        record["phase"] = phase
    _write_json(pointer_path(run_id), record)


def _complete(run_id: str, repository: Path, **kwargs) -> dict:
    return crew.complete(
        run_id,
        root=repository,
        gate="passed",
        outcome=f"{run_id} landed",
        review_waiver="these tests exercise the landing's own write",
        **kwargs,
    )


def test_the_only_node_of_a_section_closes_it_and_moves_impl(
    repository: Path, tmp_path: Path
) -> None:
    """The lone node lands, the section reads done and impl follows it."""
    run_id = "r-20261123T090000000000-only-node"
    _pointer(repository, tmp_path, run_id)

    assert _state(repository)["impl"] == 0.5, "the fixture starts half done"

    _complete(run_id, repository)

    state = _state(repository)
    assert state["section_declarations"]["s2"] == "done"
    assert (
        next(record for record in state["sections"] if record["id"] == "s2")["status"]
        == "done"
    ), "the typed record moves with its declaration"
    assert state["impl"] == 1.0, "the plan's impl moved with the declaration"


def test_a_node_whose_section_still_has_an_open_sibling_lands_untouched(
    repository: Path, tmp_path: Path
) -> None:
    """One of two nodes lands with the section left implementable."""
    first = "r-20261123T090100000000-node-a"
    second = "r-20261123T090100000001-node-b"
    _pointer(repository, tmp_path, first, node_id="node-a")
    _pointer(repository, tmp_path, second, node_id="node-b", phase="queued")

    result = _complete(first, repository)

    state = _state(repository)
    assert state["section_declarations"]["s2"] == "implementable"
    assert state["impl"] == 0.5
    assert result.get("impl_move", {}).get("verdict") in {None, "section-open"}
    # The sibling is still the section's open node, so it is untouched.
    assert pointer_path(second).exists()


def test_a_sibling_that_never_landed_does_not_hold_the_section_open(
    repository: Path, tmp_path: Path
) -> None:
    """A sibling pointer in a terminal phase is not an open node."""
    first = "r-20261123T090200000000-node-a"
    second = "r-20261123T090200000001-node-b"
    _pointer(repository, tmp_path, first, node_id="node-a")
    _pointer(repository, tmp_path, second, node_id="node-b", phase="failed")

    _complete(first, repository)

    state = _state(repository)
    assert state["section_declarations"]["s2"] == "done"
    assert state["impl"] == 1.0


def test_a_sibling_pointer_for_the_same_node_does_not_hold_it_open(
    repository: Path, tmp_path: Path
) -> None:
    """Two pointers serving one dispatch node are one node, not two."""
    first = "r-20261123T090300000000-node-a"
    second = "r-20261123T090300000001-node-a-again"
    _pointer(repository, tmp_path, first, node_id="node-a")
    _pointer(repository, tmp_path, second, node_id="node-a", phase="working")

    _complete(first, repository)

    state = _state(repository)
    assert state["section_declarations"]["s2"] == "done"
    assert state["impl"] == 1.0


def test_a_refused_landing_writes_no_declaration(
    repository: Path, tmp_path: Path
) -> None:
    """A refused last node leaves the declaration exactly as it read."""
    run_id = "r-20261123T090200000000-refused-node"
    _pointer(repository, tmp_path, run_id)
    before = _state(repository)

    with pytest.raises((crew.CrewError, ledger.LedgerError)) as refusal:
        _complete(
            run_id,
            repository,
            gate_check={
                "command": EXECUTABLE_GATE_COMMAND,
                "exit_status": 0,
            },
            require_gate_check=True,
        )

    assert "requires the check that produced it" in str(refusal.value)
    after = _state(repository)
    assert after["section_declarations"] == before["section_declarations"]
    assert after["impl"] == before["impl"] == 0.5
    assert pointer_path(run_id).exists(), "a refusal consumes nothing"
