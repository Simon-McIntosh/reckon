"""A run's cited commits are diffed once, however often its review tier is asked.

The station asks every finished run whether it still owes a review on each pass,
and the review tier behind that answer reads the run's changed paths and lines
from a git diff of its cited commits. The tier's cache key also carried the plan
file's identity, so every edit to a busy plan re-ran the diff for every run on
it. A list of full object names measures the same scope forever, so the scope is
measured once per list; a symbolic revision and an unavailable diff are not kept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import promotion_evidence as evidence

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def measured(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def measure(*, cwd: Path, commits, run_id: str = ""):
        calls.append(tuple(commits))
        return evidence._CumulativeDiff(
            ("reckon/x.py",),
            {"added": 3, "removed": 1, "available": True},
        )

    monkeypatch.setattr(evidence, "_committed_scope_uncached", measure)
    monkeypatch.setattr(
        evidence, "_COMMITTED_SCOPES", type(evidence._COMMITTED_SCOPES)()
    )
    return calls


def test_a_list_of_object_names_is_measured_once(measured, tmp_path):
    first = evidence._committed_scope(cwd=tmp_path, commits=[SHA_A, SHA_B])
    second = evidence._committed_scope(cwd=tmp_path, commits=[SHA_A, SHA_B])
    assert measured == [(SHA_A, SHA_B)]
    assert first.paths == second.paths == ("reckon/x.py",)
    assert second.changed_lines["added"] == 3


def test_a_returned_scope_cannot_change_the_kept_one(measured, tmp_path):
    first = evidence._committed_scope(cwd=tmp_path, commits=[SHA_A])
    first.changed_lines["added"] = 999
    second = evidence._committed_scope(cwd=tmp_path, commits=[SHA_A])
    assert second.changed_lines["added"] == 3


def test_a_symbolic_revision_is_measured_every_time(measured, tmp_path):
    evidence._committed_scope(cwd=tmp_path, commits=["HEAD"])
    evidence._committed_scope(cwd=tmp_path, commits=["HEAD"])
    assert measured == [("HEAD",), ("HEAD",)]


def test_an_unavailable_diff_is_measured_again(measured, monkeypatch, tmp_path):
    def unavailable(*, cwd: Path, commits, run_id: str = ""):
        measured.append(tuple(commits))
        return evidence._CumulativeDiff((), {"available": False, "reason": "git"})

    monkeypatch.setattr(evidence, "_committed_scope_uncached", unavailable)
    evidence._committed_scope(cwd=tmp_path, commits=[SHA_A])
    evidence._committed_scope(cwd=tmp_path, commits=[SHA_A])
    assert measured == [(SHA_A,), (SHA_A,)]
