"""Stream enumeration is one implementation, shared by its former callers.

``reckon.crew.metering.run_streams`` is the single enumerator. Every expected
list below is the list the pre-reuse helper returned at base revision
24df7b57b, except the lane-change rows, which record the one intended change:
a run's lane changes now sit beside its stream and resumes instead of being
dropped.
"""

from __future__ import annotations

from pathlib import Path

from reckon import ledger
from reckon.crew import metering, promotion


def _build_tree(root: Path) -> None:
    only = root / "only"
    only.mkdir()
    (only / "stream.jsonl").write_text("")

    resumed = root / "resumed"
    resumed.mkdir()
    for name in ("stream.jsonl", "resume-2.jsonl", "resume-10.jsonl"):
        (resumed / name).write_text("")

    laned = root / "laned"
    laned.mkdir()
    for name in (
        "stream.jsonl",
        "resume-2.jsonl",
        "resume-10.jsonl",
        "lane-change-1.jsonl",
    ):
        (laned / name).write_text("")


def test_promotion_returns_the_single_stream(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    only = tmp_path / "only"
    assert promotion._run_streams(only / "stream.jsonl") == [only / "stream.jsonl"]


def test_promotion_orders_resumes_numerically(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    resumed = tmp_path / "resumed"
    # base revision 24df7b57b: stream, then resume-2 before resume-10 by turn.
    assert promotion._run_streams(resumed / "stream.jsonl") == [
        resumed / "stream.jsonl",
        resumed / "resume-2.jsonl",
        resumed / "resume-10.jsonl",
    ]


def test_promotion_resolves_stream_from_a_resume_path(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    resumed = tmp_path / "resumed"
    # a path naming a resume file still enumerates the whole run.
    assert promotion._run_streams(resumed / "resume-10.jsonl") == [
        resumed / "stream.jsonl",
        resumed / "resume-2.jsonl",
        resumed / "resume-10.jsonl",
    ]


def test_promotion_returns_nothing_for_a_missing_directory(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    missing = tmp_path / "missing"
    assert promotion._run_streams(missing / "stream.jsonl") == []


def test_promotion_includes_lane_changes(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    laned = tmp_path / "laned"
    # base revision 24df7b57b list, plus the lane change this reuse adds.
    assert promotion._run_streams(laned / "stream.jsonl") == [
        laned / "stream.jsonl",
        laned / "resume-2.jsonl",
        laned / "resume-10.jsonl",
        laned / "lane-change-1.jsonl",
    ]


def test_ledger_returns_the_single_stream(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    only = tmp_path / "only"
    assert ledger._run_streams("only", tmp_path) == [only / "stream.jsonl"]


def test_ledger_orders_resumes_numerically(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    resumed = tmp_path / "resumed"
    # base revision 24df7b57b: stream, then resume-2 before resume-10 by turn.
    assert ledger._run_streams("resumed", tmp_path) == [
        resumed / "stream.jsonl",
        resumed / "resume-2.jsonl",
        resumed / "resume-10.jsonl",
    ]


def test_ledger_caller_returns_nothing_for_a_missing_directory(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    assert ledger._run_streams("missing", tmp_path) == []


def test_ledger_includes_lane_changes(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    laned = tmp_path / "laned"
    # base revision 24df7b57b list, plus the lane change this reuse adds.
    assert ledger._run_streams("laned", tmp_path) == [
        laned / "stream.jsonl",
        laned / "resume-2.jsonl",
        laned / "resume-10.jsonl",
        laned / "lane-change-1.jsonl",
    ]


def test_both_callers_agree(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    for run_id in ("only", "resumed", "laned"):
        directory = tmp_path / run_id
        assert promotion._run_streams(
            directory / "stream.jsonl"
        ) == ledger._run_streams(run_id, tmp_path)


def test_the_shared_helper_delegates(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    directory = tmp_path / "laned"
    assert promotion._run_streams(directory / "stream.jsonl") == metering.run_streams(
        directory / "stream.jsonl"
    )
