"""A review dispatched by hand is the run's review.

A run review declares the record paths it will write inside the directory of
the run it reviews, so its fence reaches no other run's record. Recognition
therefore keys on those granted paths: a pointer whose write paths are the ones
the composer declares for run R at head H is R's standing review, whatever node
id a coordinator chose when dispatching it by hand, and whether the lane has
launched it or is still holding it in its queue. A review that read an earlier
revision is not R's review at the head R carries now, and a figure review is not
the content review a scoring run owes.

Every fixture path is built by calling the composer, so this file fails if
either the composition or the recognition moves.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import queue_admission, runs
from tests.helpers.git import git as _git

subject_module = importlib.import_module("reckon.crew.recovery_review_subject")
recovery = importlib.import_module("reckon.crew.recovery_review_dispatch")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "hand-review-fixture"
SESSION = "coordinator-fixture"
OBSERVED_AT = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

SOURCE = "run-repaired"
REVIEW = "run-hand-review"


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed hand-review fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return root


def _write_pointer(run_id: str, record: dict) -> None:
    runs._write_json(runs.pointer_path(run_id), record)


def _source_pointer(repository: Path) -> dict:
    """The reviewed run's own pointer, owed a review at its current head."""
    _write_pointer(
        SOURCE,
        {
            "run_id": SOURCE,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(repository),
            "worktree": str(repository),
            "node": {
                "id": "source-node",
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )
    return runs.read_pointer(SOURCE)


def _composed(record: dict, *, rubric: str | None = None) -> tuple[list[str], str]:
    """The record paths the composer declares for this run, and the head."""
    fields = subject_module._review_dispatch_fields(record, rubric=rubric)
    return list(fields["write_paths"]), str(fields.get("head") or "")


def _review_pointer(
    paths: list[str], *, run_id: str = REVIEW, phase: str = ""
) -> None:
    """A hand-dispatched review: a coordinator's own node id, no review role."""
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": True,
        "node": {
            "id": "coordinator-own-review",
            "plan": "fixture-plan",
            "section": "fixture-section",
            "write_paths": list(paths),
        },
    }
    if phase:
        record["phase"] = phase
        record["queued_at"] = OBSERVED_AT.isoformat()
    _write_pointer(run_id, record)


def _row(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "session": SESSION,
        "plan": "fixture-plan",
        "node": "source-node",
        "classification": "scoring",
        "recovery_classification": "scoring",
        "terminal_age_seconds": 120,
        "next_action": "reckon crew dispatch --node review-of-source-node",
    }


def _duty_kinds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    monkeypatch.setattr(
        obligations_module, "_classified_rows", lambda _project: [_row(SOURCE)]
    )
    monkeypatch.setattr(
        runs, "drain", lambda *_args, **_kwargs: {"unreconciled_runs": 1}
    )
    result = obligations_module.obligations(PROJECT, SESSION)
    return [item["kind"] for item in result["obligations"]]


def test_a_live_review_pointer_naming_the_runs_paths_is_in_flight(
    repository: Path,
) -> None:
    """A hand dispatch with the composer's paths is this run's standing review."""
    record = _source_pointer(repository)
    paths, head = _composed(record)
    # The head-keyed path is composed only when the head resolves, so its
    # presence is the control that this fixture exercises the whole composition
    # rather than the headless spelling alone.
    assert head and len(paths) == 2, (head, paths)
    assert recovery.REVIEW_DISPATCH_FIELD not in record

    _review_pointer(paths)

    assert recovery._review_in_flight(runs.read_pointer(SOURCE)) == REVIEW


def test_a_queued_review_pointer_makes_the_duty_read_queued(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A review the lane holds is a wait on the run's duty, not a missing review."""
    record = _source_pointer(repository)
    paths, head = _composed(record)
    assert head and paths
    _review_pointer(paths, phase=queue_admission.QUEUED_PHASE)

    # A queued review has not launched, so it does not suppress the duty.
    assert obligations_module._current_review_in_flight(record) is False

    kinds = _duty_kinds(monkeypatch)
    assert "review-queued" in kinds, kinds
    assert "review-missing" not in kinds, kinds


def test_a_review_of_an_earlier_head_is_not_the_review_at_this_head(
    repository: Path,
) -> None:
    """Matching on the run alone would let a stale review answer for a moved head."""
    record = _source_pointer(repository)
    earlier_paths, earlier_head = _composed(record)
    assert earlier_head and earlier_paths

    (repository / "seed.txt").write_text("changed\n", encoding="utf-8")
    for arguments in (
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: move the reviewed head"),
    ):
        _git(repository, *arguments)

    record = runs.read_pointer(SOURCE)
    current_paths, current_head = _composed(record)
    assert current_head and current_head != earlier_head
    assert current_paths != earlier_paths

    _review_pointer(earlier_paths)
    assert recovery._review_in_flight(record) == ""

    # Control: the same pointer with the current head's paths is recognised, so
    # the negative above is about the head and not about the pointer's shape.
    _review_pointer(current_paths)
    assert recovery._review_in_flight(record) == REVIEW


def test_a_figure_review_is_told_apart_from_a_content_review(
    repository: Path,
) -> None:
    """A figure review must not answer for the content review a run owes."""
    record = _source_pointer(repository)
    figure_paths, head = _composed(record, rubric="figure")
    content_paths, _ = _composed(record)
    assert head and figure_paths and figure_paths != content_paths

    _review_pointer(figure_paths, run_id="run-figure-review")
    assert recovery._review_in_flight(record, rubric="figure") == "run-figure-review"
    assert recovery._review_in_flight(record) == ""

    _review_pointer(content_paths)
    assert recovery._review_in_flight(record) == REVIEW
    assert recovery._review_in_flight(record, rubric="figure") == "run-figure-review"