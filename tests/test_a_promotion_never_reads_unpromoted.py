"""A promoted run reads promoted from the promote record, never unpromoted.

Promotion appends the run's ledger row and then removes the live pointer. A
classifier that reads the pointer alone calls the run unpromoted for the whole
of that window: the landing commit moves the tree's head, so the stored review
of the head being classified no longer matches and the completed run is read as
awaiting a review it already has. The follower then renders a landing as
unfinished work — the measured ``complete -> unpromoted`` flicker — before the
pointer's absence and the ledger row finally agree on ``promoted``.

The ledger row is the fleet's evidence that the work landed, and it exists
before the pointer is removed. The first test drives a synthesized completed run
through promotion and classifies it at exactly the boundary between the two
writes. The rest hold the three departure routes that leave a pointer behind
without a recorded promotion to the word each still earns.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion, recovery
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path, read_pointer, run_dir

PROJECT = "proj"
PLAN = "plan-a"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Resolve every crew directory under the pytest temporary home.

    The worker git shim refuses a mutating git verb that targets a repository
    other than the running worker's own worktree, and promotion commits the
    stores it writes into the repository under test in the test's temporary
    directory. The shim takes its scope from ``RECKON_RUN_ID``, so the
    subprocesses this module spawns run without it and reach the real git.
    """
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.delenv("RECKON_RUN_ID", raising=False)
    return config_home


@pytest.fixture()
def repository(tmp_path: Path, home: Path) -> Path:
    """A committed checkout that a promotion can land its stores into."""
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    # Promotion commits the stores it writes, so a fixture that promotes must
    # be a git worktree with a committed head for the landing to land into.
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_completed_pointer(
    repository: Path,
    tmp_path: Path,
    run_id: str,
    *,
    role: str = "implement",
) -> None:
    """A completed run reporting a report-only delivery, as a live pointer."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: node-a\n"
        "status: complete\n"
        "changed_paths: none (the sole deliverable is the report)\n"
        "tests: focused promotion check passed\n",
        encoding="utf-8",
    )
    # Dispatch creates the run's own home, which is where a discard leaves its
    # marker; the run's home is a different crew directory from the live
    # pointer's, so writing the pointer alone does not create it.
    run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "role": role,
            "backend": "native",
            "created_at": "2026-09-26T13:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s2",
                "time_budget": "25m",
                "write_paths": [],
            },
        },
    )


def _store_review(run_id: str, head: str) -> None:
    """Store a parsed review naming the head it read, the way production does."""
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
        }
    )
    review_module.store_review(record)


def _snapshot(run_id: str) -> dict:
    return recovery._watch_snapshot(
        read_pointer(run_id), moment=time.time(), stall_seconds=3600
    )


def test_a_promoted_run_reads_promoted_at_the_promote_boundary(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instant between the promote record and the pointer removal is promoted."""
    run_id = "r-20260926T133400000000-node-a"
    _write_completed_pointer(repository, tmp_path, run_id)
    _store_review(run_id, _git(repository, "rev-parse", "HEAD"))

    before = _snapshot(run_id)
    assert before["state"] == "complete"
    assert before["classification"] == "promotable"

    observed = []
    original = promotion._capture_member_session

    def boundary_hook(record):
        snap = _snapshot(run_id)
        observed.append((snap["state"], snap["classification"]))
        return original(record)

    monkeypatch.setattr(promotion, "_capture_member_session", boundary_hook)

    crew.complete(run_id, gate="passed", root=repository)

    assert observed, "the promotion never reached the promote-record boundary"
    assert len(observed) == 1
    assert observed[0] == ("promoted", "promoted")
    assert ledger.run_path(PROJECT, run_id, repository).is_file()
    assert not pointer_path(run_id).exists()


# ── the departure routes that never earn the promoted word ──────────────────


def _departure_word_for(run_id: str, snapshot: dict, *, recorded: bool) -> str:
    """The word a fold gives a run when the caller supplies the ledger reader."""

    def recorded_ids() -> set[str]:
        return {run_id} if recorded else set()

    events, _running = recovery.fleet_transitions(
        {run_id: snapshot},
        {},
        ledger_run_ids=recorded_ids,
    )
    return events[0][2]


def _departure_word_from_ledger(run_id: str, snapshot: dict) -> str:
    """The word a fold gives a run when the caller supplies no reader.

    The fold must resolve its own reader from the departing run's project, so
    the project's ledger — environment-resolved under the temporary home — is
    what decides between a landing and a pointer that vanished.
    """
    events, _running = recovery.fleet_transitions({run_id: snapshot}, {})
    return events[0][2]


def _departing_snapshot(run_id: str, state: str = "dispatched") -> dict:
    """A fleet row for a run about to leave, naming only its own project."""
    return {
        "run_id": run_id,
        "project": PROJECT,
        "session": "",
        "state": state,
        "recovery_classification": state,
        "detail": "",
        "needs_help_complete": None,
    }


def _record_project_run(run_id: str) -> None:
    """Write a promoted run's row into the project's own ledger."""
    row = ledger.run_path(PROJECT, run_id)
    row.parent.mkdir(parents=True, exist_ok=True)
    _write_json(row, {"run_id": run_id, "project": PROJECT})


def test_a_refused_dispatch_departs_withdrawn(repository: Path) -> None:
    """A dispatch refused at admission leaves no row, so it departs withdrawn."""
    run_id = "r-20260926T133500000000-refused"
    snapshot = _departing_snapshot(run_id, state="abandoned")

    assert not ledger.run_path(PROJECT, run_id, repository).is_file()
    assert _departure_word_for(run_id, snapshot, recorded=False) == "withdrawn"


def test_a_vanished_reflex_review_departs_withdrawn(repository: Path) -> None:
    """A review pointer that vanishes with no record departs withdrawn."""
    run_id = "r-20260926T133600000000-review-of-node-a"
    snapshot = _departing_snapshot(run_id, state="complete")

    assert not ledger.run_path(PROJECT, run_id, repository).is_file()
    assert _departure_word_for(run_id, snapshot, recorded=False) == "withdrawn"


def test_a_discard_departs_discarded(repository: Path, tmp_path: Path) -> None:
    """A deliberate discard is named for what it was, never as a withdrawal."""
    run_id = "r-20260926T133700000000-discard"
    _write_completed_pointer(repository, tmp_path, run_id)
    snapshot = _snapshot(run_id)

    crew.discard(run_id)

    assert _departure_word_for(run_id, snapshot, recorded=False) == "discarded"


def test_the_departure_control_states_promoted_only_for_a_recorded_row(
    repository: Path,
) -> None:
    """The control: the same fold reads promoted when the ledger records the run."""
    run_id = "r-20260926T133800000000-landed"
    snapshot = _departing_snapshot(run_id, state="complete")

    assert _departure_word_for(run_id, snapshot, recorded=True) == "promoted"
    assert _departure_word_for(run_id, snapshot, recorded=False) == "withdrawn"


def test_the_fold_resolves_an_unsupplied_reader_from_the_project_ledger(
    repository: Path, tmp_path: Path
) -> None:
    """No reader supplied: production resolves one from the run's own project.

    The departing run's snapshot names only its project, so the fold must reach
    that project's own ledger — environment-resolved under the temporary home —
    to tell a landing from a pointer that vanished. Each departure word must
    still be the exact one the section names.
    """
    landed = "r-20260926T133900000000-landed"
    _record_project_run(landed)
    assert (
        _departure_word_from_ledger(
            landed, _departing_snapshot(landed, state="complete")
        )
        == "promoted"
    )

    vanished = "r-20260926T134000000000-vanished"
    assert (
        _departure_word_from_ledger(
            vanished, _departing_snapshot(vanished, state="abandoned")
        )
        == "withdrawn"
    )

    discarded = "r-20260926T134100000000-discarded"
    _write_completed_pointer(repository, tmp_path, discarded)
    crew.discard(discarded)
    assert (
        _departure_word_from_ledger(
            discarded, _departing_snapshot(discarded, state="complete")
        )
        == "discarded"
    )
