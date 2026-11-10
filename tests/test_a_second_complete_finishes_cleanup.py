"""A second complete finishes what an interrupted promotion left behind.

A promotion writes the plan comment, then the ledger row, then retires the live
pointer and releases the worktree. A landing that stops after the row leaves a
run that is promoted by the record and unreconciled by every reader that looks
at the live directory: no recovery verb clears it, because the row already
exists. A second complete must therefore finish the remaining cleanup instead
of refusing, and it must not record the run again.

The narrative is the part a second call cannot repeat byte-identically. The
coordinator who retries describes the outcome in their own words, and the plan
comment written by the first promotion already holds another. The committed
ledger row is the record of that promotion, so the difference is reported in
the receipt while the existing comment is kept.

Two cases run in a temporary config home: a run whose row is written and whose
pointer survived is retired with no second row, and the same run retried with a
different narrative finishes the cleanup and names the difference. The third is
the ordinary promotion of a run with no row, which still writes one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew.runs import _write_json, pointer_path
from tests.helpers.git import git as _git

PROJECT = "sample"
PLAN = "plan-a"


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
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthetic config home and committed repository, isolated from the fleet."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
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
            "impl": 0.0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _pointer(repository: Path, run_id: str) -> None:
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": "",
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-10-01T06:00:00Z",
            "manifest_path": "",
            "node": {
                "id": "node-a",
                "plan": PLAN,
                "section": "s1",
                "write_paths": [],
            },
        },
    )


def _promote(repository: Path, run_id: str, *, outcome: str) -> dict:
    return crew.complete(run_id, gate="not-run", outcome=outcome, root=repository)


def _landing_comment_bodies(repository: Path) -> list[str]:
    state = _plan_html.read_state_file(repository / "docs" / "plans" / f"{PLAN}.html")
    return [
        item.get("body", "")
        for items in (state.get("comments") or {}).values()
        for item in items
    ]


# ── Case 1: a written row and a surviving pointer ───────────────────────────


def test_a_second_complete_retires_the_pointer_and_writes_no_second_row(
    repository: Path,
) -> None:
    """The cleanup a promotion left undone is finished by the next call."""
    run_id = "r-second-complete-same-narrative"
    outcome = "reviewed and landed before the pointer was retired"
    _pointer(repository, run_id)
    first = _promote(repository, run_id, outcome=outcome)
    assert first["already_promoted"] is False
    assert len(ledger.runs(PROJECT, root=repository)) == 1

    # The state the interrupted promotion leaves: row committed, pointer still
    # on disk, so the run reads as live to every reader of the live directory.
    _pointer(repository, run_id)
    second = _promote(repository, run_id, outcome=outcome)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1
    assert second["plan_comment"]["finished_cleanup"] is True
    assert "cleanup" in second["plan_comment"]["note"]


# ── Case 2: the retry's narrative differs from the recorded one ─────────────


def test_a_differing_narrative_finishes_the_cleanup_and_is_named(
    repository: Path,
) -> None:
    """The row is the record, so a new narrative is reported, not refused."""
    run_id = "r-second-complete-other-narrative"
    recorded = "reviewed and landed before the pointer was retired"
    retried = "relearning the landing in the coordinator's own words"
    _pointer(repository, run_id)
    _promote(repository, run_id, outcome=recorded)
    _pointer(repository, run_id)

    second = _promote(repository, run_id, outcome=retried)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1
    comment = second["plan_comment"]
    assert comment["finished_cleanup"] is True
    assert comment["reason"] == "different_narrative"
    assert comment["kept_narrative"] == recorded
    assert comment["requested_narrative"] == retried
    assert recorded in comment["note"]
    # The comment keeps the narrative the first promotion wrote.
    assert any(recorded in body for body in _landing_comment_bodies(repository))
    assert not any(retried in body for body in _landing_comment_bodies(repository))


# ── Case 3: no row yet, the ordinary promotion ──────────────────────────────


def test_a_run_with_no_ledger_row_is_promoted(
    repository: Path,
) -> None:
    """A run that never landed records its row and retires its pointer."""
    run_id = "r-first-promotion"
    _pointer(repository, run_id)

    receipt = _promote(repository, run_id, outcome="landed for the first time")

    assert receipt["already_promoted"] is False
    assert not pointer_path(run_id).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert len(rows) == 1
    assert receipt["plan_comment"]["recorded"] is True
    assert "finished_cleanup" not in receipt["plan_comment"]
