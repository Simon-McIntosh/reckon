"""The obligations hook lists the duties a coordinator can act on.

A session's list mixes work it must do with waits nothing at this session can
shorten: requests queued on a session's upstream section or a lane's own
admission pass, and reviews an armed reflex has already taken. Printing them
turn after turn buries the few actionable ones, so the hook prints the
actionable rows in full and counts the rest on one line. A stop is held open
only by that same actionable set, because refusing a turn over a wait the
coordinator cannot shorten asks for work nobody can do.

The second half is the other direction: a review the reflex has already tried
and been refused is not a wait at all. The refusal names the one answer that
unblocks it, and the duty carries that answer rather than a window that will
pass while the run stands still.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew import runs
from reckon.crew.query import project_run_rows
from reckon.hooks.coordinator_obligations import _blocking_items, format_checklist
from tests.helpers.git import git as _git

obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "actionable-rows-fixture"
SESSION = "actionable-rows-session"
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
BLOCKING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "the guard never fires",
    "severity": review_module.BLOCKING_FINDING_SEVERITY,
}
REFLEX_WINDOW = 120


def _row(kind: str, *, run_id: str, age: str | int, **extra: object) -> dict:
    """One duty row of the shape the snapshot hands the hook."""
    row = {
        "kind": kind,
        "run_id": run_id,
        "node": f"{run_id}-node",
        "age_seconds": age,
        "next_command": f"reckon crew resume --run {run_id}",
    }
    row.update(extra)
    return row


def _payload(*rows: dict) -> dict:
    ages = [int(row["age_seconds"]) for row in rows]
    return {
        "project": PROJECT,
        "session": SESSION,
        "obligations": list(rows),
        "summary": {
            "count": len(rows),
            "oldest_age_seconds": max(ages, default=0),
            "unreconciled_runs": 0,
        },
    }


def _summary_lines(rendered: str) -> list[str]:
    """The collapsed summary lines, told from the full ones by their wording."""
    return [line for line in rendered.splitlines() if "no action shortens them" in line]


def _waiting_rows() -> tuple[dict, dict, dict]:
    """One row of each kind the hook must not print in full."""
    return (
        _row("run-queued", run_id="queued-run", age=120),
        _row("review-queued", run_id="queued-review", age=12),
        _row(
            "review-missing",
            run_id="reflex-review",
            age=34,
            review_owner="reflex",
            reflex_window_seconds=REFLEX_WINDOW,
        ),
    )


def test_the_waits_collapse_to_one_summary_line_and_hold_no_stop() -> None:
    """Three kinds of wait print as one line, and the stop verdict is empty.

    ``_blocking_items`` is the verdict the stop path reads: an empty list is
    the hook allowing the turn to end, which is what makes this a stop
    assertion rather than a formatting one.
    """
    payload = _payload(*_waiting_rows())

    rendered = format_checklist(payload)

    summaries = _summary_lines(rendered)
    assert len(summaries) == 1, rendered
    summary = summaries[0]
    assert "run-queued 1" in summary, summary
    assert "review-queued 1" in summary, summary
    assert "review-missing 1" in summary, summary
    for run_id in ("queued-run", "queued-review", "reflex-review"):
        assert run_id not in rendered, rendered
    assert _blocking_items(payload["obligations"]) == []


def test_one_actionable_row_prints_in_full_and_holds_the_stop() -> None:
    """Adding a promotable row prints it whole, and the stop is held."""
    promotable = _row("promotable", run_id="promotable-run", age=5)
    payload = _payload(*_waiting_rows(), promotable)

    rendered = format_checklist(payload)

    assert "- [promotable] promotable-run" in rendered, rendered
    assert len(_summary_lines(rendered)) == 1, rendered
    assert _blocking_items(payload["obligations"]) == [promotable]


def test_a_reflex_row_collapses_until_its_window_has_passed() -> None:
    """The window is the boundary: at it the row is the coordinator's again."""
    at_window = _row(
        "review-missing",
        run_id="at-window",
        age=REFLEX_WINDOW,
        review_owner="reflex",
        reflex_window_seconds=REFLEX_WINDOW,
    )
    inside = _row(
        "review-missing",
        run_id="inside-window",
        age=REFLEX_WINDOW - 1,
        review_owner="reflex",
        reflex_window_seconds=REFLEX_WINDOW,
    )

    at_rendered = format_checklist(_payload(at_window))
    inside_rendered = format_checklist(_payload(inside))

    assert "at-window" in at_rendered, at_rendered
    assert _summary_lines(at_rendered) == []
    assert _blocking_items([at_window]) == [at_window]
    assert "inside-window" not in inside_rendered, inside_rendered
    assert len(_summary_lines(inside_rendered)) == 1, inside_rendered
    assert _blocking_items([inside]) == []


# ── The derivation side: a refusal is not a wait ─────────────────────────────


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    _commit(root, "seed.txt", "seed\n")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _delivered_run(repository: Path, tmp_path: Path, *, run_id: str, head: str) -> dict:
    """One complete, unpromoted run whose tree carries ``head``."""
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: {run_id}-node\nstatus: complete\ncommits: [{head}]\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "base_sha": head,
        "process_alive": False,
        "launch": "in-harness",
        "role": "implement",
        "manifest_path": str(manifest),
        "node": {
            "id": f"{run_id}-node",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "time_budget": "20m",
            "write_paths": ["seed.txt"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _earlier_review_with_a_blocking_finding(
    repository: Path, *, run_id: str, marker: str
) -> None:
    """Store a review of the tree's current head carrying one blocking finding.

    The run's head is moved past it afterwards, so the stored blocking finding
    is the one a dispatch over the new head would repeat — the state in which
    the reflex records its refusal — while the duty reads review-missing for
    the revision the run now carries.
    """
    reviewed = _git(repository, "rev-parse", "HEAD")
    emitted = "\n".join(
        f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
    )
    record = review_module.parse_review(emitted)
    record.update(
        {
            "project": PROJECT,
            "reviewed_run_id": run_id,
            "review_run_id": f"review-of-{run_id}",
            "reviewed_base_sha": reviewed,
            "reviewed_head_sha": reviewed,
            "findings": [dict(BLOCKING)],
        }
    )
    review_module.store_review(record)
    _commit(repository, "reckon/repair_source.py", f"VALUE = {marker!r}\n")


def _record_refusal(run_id: str, *, at: datetime) -> dict:
    """Write the refusal the reflex records on the run it acted for."""
    pointer = runs.read_pointer(run_id)
    pointer["review_dispatch"] = {
        "status": "refused",
        "reason": (
            f"run {run_id!r} cannot be reviewed: its newest review carries 1 "
            "blocking finding(s) with no disposition"
        ),
        "run_id": None,
        "backend": None,
        "head": None,
        "at": at.isoformat(),
        "attempt": 9,
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _duty(result: dict) -> dict:
    duties = [
        item for item in result["obligations"] if item["kind"] == "review-missing"
    ]
    assert len(duties) == 1, [
        (item["kind"], item["run_id"]) for item in result["obligations"]
    ]
    return duties[0]


def test_a_refusal_after_delivery_names_the_answer_that_lifts_it(
    repository: Path, tmp_path: Path
) -> None:
    """The refusal, not the reflex, owns the row while it speaks for the head."""
    run_id = "run-refused-after-delivery"
    _earlier_review_with_a_blocking_finding(repository, run_id=run_id, marker="1")
    head = _git(repository, "rev-parse", "HEAD")
    _delivered_run(repository, tmp_path, run_id=run_id, head=head)
    pointer = _record_refusal(run_id, at=OBSERVED_AT)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    duty = _duty(result)
    assert "review_owner" not in duty, duty
    assert duty["reason"] == pointer["review_dispatch"]["reason"], duty
    fold, exempt = (
        obligations_module.recovery_review_dispatch._finding_dispose_commands(
            PROJECT, run_id, 0
        )
    )
    assert fold in duty["next_command"], duty
    assert exempt in duty["next_command"], duty


def test_a_refusal_from_before_the_delivery_leaves_the_reflex_the_owner(
    repository: Path, tmp_path: Path
) -> None:
    """A refusal older than the run's last delivery says nothing about this head."""
    run_id = "run-refused-before-delivery"
    _earlier_review_with_a_blocking_finding(repository, run_id=run_id, marker="2")
    head = _git(repository, "rev-parse", "HEAD")
    _delivered_run(repository, tmp_path, run_id=run_id, head=head)
    _record_refusal(run_id, at=OBSERVED_AT - timedelta(hours=1))

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    duty = _duty(result)
    assert duty["review_owner"] == "reflex", duty
    assert (
        duty["reflex_window_seconds"]
        == obligations_module._reflex_review_window_seconds()
    )


def test_no_refusal_leaves_the_reflex_the_owner_with_its_window(
    repository: Path, tmp_path: Path
) -> None:
    """Absent a refusal, both reflex fields survive the published projection."""
    run_id = "run-no-refusal"
    head = _commit(repository, "reckon/repair_module.py", "REPAIR = 3\n")
    _delivered_run(repository, tmp_path, run_id=run_id, head=head)

    with runs.follower_claim(PROJECT, SESSION, delivery="stream"):
        result = obligations_module.obligations(PROJECT, SESSION)

    duty = _duty(result)
    window = obligations_module._reflex_review_window_seconds()
    assert duty["review_owner"] == "reflex", duty
    assert duty["reflex_window_seconds"] == window, duty
    assert "reckon crew dispatch" not in duty["next_command"], duty

    projected = project_run_rows(
        result["obligations"],
        fields=obligations_module.DUTY_ROW_FIELDS,
        view="obligations",
        vocabulary=obligations_module.DUTY_ROW_FIELDS,
    )
    published = [row for row in projected if row["kind"] == "review-missing"]
    assert len(published) == 1, projected
    assert published[0]["review_owner"] == "reflex", published[0]
    assert published[0]["reflex_window_seconds"] == window, published[0]
