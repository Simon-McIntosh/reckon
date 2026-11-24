"""Review coverage reads each fact that cannot change once per process.

An admission pass judges whether each queued plan owes a review, and that
judgement computes the plan's review coverage. On 24 November one coverage
computation for a large plan took 204 seconds: it walked the plan file's whole
history once per stored finding to find the commit that added a reviewed blob,
and asked git whether one commit descends from another inside a sort
comparator. Each of those answers is fixed for its inputs, so a later
computation reads none of them again; a lookup that found nothing is asked
again, because the plan may be committed later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew import plan_review

BLOB = "a" * 40
COMMIT = "c" * 40


class _Asked(list):
    """The git lookups a case made, and the answer the stand-in gives."""

    answers: dict[str, str]


@pytest.fixture(autouse=True)
def _empty_memos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plan_review, "_PLAN_COMMIT_FOR_BLOB", {})
    monkeypatch.setattr(plan_review, "_SETTLED_ANCESTRY", {})


@pytest.fixture()
def located(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Asked:
    monkeypatch.setattr(
        plan_review,
        "_plan_location",
        lambda project, slug, path: (tmp_path, tmp_path / "plan.html", "plan.html"),
    )
    asked = _Asked()
    answers = {"output": f"{COMMIT}\n"}

    def git(repo, *args):
        asked.append(args)
        return answers["output"]

    monkeypatch.setattr(plan_review, "_git", git)
    asked.answers = answers
    return asked


def test_a_found_commit_is_looked_up_once(located) -> None:
    for _ in range(3):
        assert plan_review.plan_commit_for("p", "plan", BLOB) == COMMIT
    assert len(located) == 1


def test_a_blob_with_no_commit_is_asked_again(located) -> None:
    located.answers["output"] = ""
    assert plan_review.plan_commit_for("p", "plan", BLOB) is None
    located.answers["output"] = f"{COMMIT}\n"
    assert plan_review.plan_commit_for("p", "plan", BLOB) == COMMIT
    assert len(located) == 2


def test_a_settled_ancestry_answer_is_kept_across_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from reckon import velocity

    asked: list[tuple[str, str]] = []

    def is_ancestor(repo, ancestor, descendant):
        asked.append((ancestor, descendant))
        return True

    monkeypatch.setattr(velocity, "_is_ancestor", is_ancestor)
    for _ in range(3):
        assert plan_review._commit_is_ancestor(tmp_path, "a" * 40, "b" * 40) is True
    assert asked == [("a" * 40, "b" * 40)]


def test_an_unknown_ancestry_answer_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from reckon import velocity

    asked: list[tuple[str, str]] = []

    def is_ancestor(repo, ancestor, descendant):
        asked.append((ancestor, descendant))

    monkeypatch.setattr(velocity, "_is_ancestor", is_ancestor)
    plan_review._commit_is_ancestor(tmp_path, "a" * 40, "b" * 40)
    plan_review._commit_is_ancestor(tmp_path, "a" * 40, "b" * 40)
    assert len(asked) == 2
