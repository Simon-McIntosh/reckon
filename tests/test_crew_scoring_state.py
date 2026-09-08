"""Independent-review state transitions for completed crew runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import recovery, review


@pytest.fixture()
def review_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the durable review store from the operator's crew state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _pointer(review_home: Path, *, with_manifest: bool = True) -> dict:
    manifest = review_home / "runs" / "source-run" / "manifest.md"
    if with_manifest:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            "node: source-node\nstatus: complete\ncommits: abc123\n",
            encoding="utf-8",
        )
    return {
        "run_id": "source-run",
        "project": "sample-project",
        "session": "sample-session",
        "node": {
            "id": "source-node",
            "plan": "delivery-plan",
            "section": "delivery",
        },
        "phase": "complete",
        "manifest_path": str(manifest),
        "manifest_baseline_mtime_ns": 0,
        "log_path": str(review_home / "runs" / "source-run" / "stream.jsonl"),
        "process_alive": False,
    }


def _parsed_review() -> dict:
    emitted = "\n".join(
        f"SCORE {dimension}: 18" for dimension in review.REVIEW_DIMENSIONS
    )
    record = review.parse_review(emitted)
    record.update(
        {
            "project": "sample-project",
            "reviewed_run_id": "source-run",
            "review_run_id": "review-run",
        }
    )
    return record


def test_completed_manifest_waits_in_scoring_without_review(review_home: Path) -> None:
    row = recovery.classify_pointer(_pointer(review_home))

    assert row["classification"] == "scoring"
    assert row["classification"] != "completed_unpromoted"
    assert row["recovery"] == "review"
    assert row["review_present"] is False
    assert row["review_status"] is None


def test_parsed_review_makes_completed_manifest_promotable(review_home: Path) -> None:
    stored = review.store_review(_parsed_review())

    row = recovery.classify_pointer(_pointer(review_home))

    assert stored == review.review_path("sample-project", "source-run")
    assert row["classification"] == "promotable"
    assert row["recovery"] == "promote"
    assert row["review_present"] is True
    assert row["review_status"] == "parsed"
    assert row["next_action"].startswith("reckon crew complete --run source-run")


def test_missing_manifest_keeps_existing_abandoned_classification(
    review_home: Path,
) -> None:
    row = recovery.classify_pointer(_pointer(review_home, with_manifest=False))

    assert row["classification"] == "abandoned"
    assert row["recovery"] == "recover"
    assert row["review_present"] is False
    assert row["review_status"] is None


def test_scoring_action_dispatches_the_named_review_worker(review_home: Path) -> None:
    row = recovery.classify_pointer(_pointer(review_home))

    action = row["next_action"]
    assert action.startswith("reckon crew dispatch ")
    assert "--role review" in action
    assert "--node review-of-source-node" in action
    assert "--goal 'attach an independent review to run source-run'" in action
    assert "--local" in action


def test_unparsed_review_is_distinct_from_absent_review(review_home: Path) -> None:
    absent = recovery.classify_pointer(_pointer(review_home))
    record = review.parse_review("review output without structured scores")
    record.update(
        {
            "project": "sample-project",
            "reviewed_run_id": "source-run",
            "review_run_id": "review-run",
        }
    )
    review.store_review(record)

    unparsed = recovery.classify_pointer(_pointer(review_home))

    assert absent["classification"] == unparsed["classification"] == "scoring"
    assert absent["review_present"] is False
    assert absent["review_status"] is None
    assert unparsed["review_present"] is True
    assert unparsed["review_status"] == "unparsed"
    assert (
        review.read_review("sample-project", "source-run")["raw_text"]
        == record["raw_text"]
    )
