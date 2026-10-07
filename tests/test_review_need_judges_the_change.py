"""A change since a review is judged for whether it needs a new review.

The judge reads the diff since the review, what the review checks and the goals
the work serves, so it can cover a large rewording and uncover a small change of
substance, which a share of changed words cannot tell apart. Every case stubs
the Jev client, so nothing here reaches the live service; with the stub absent
the suite's isolated credential makes the judge unanswered and the word-share
rule decides, which the significant-change cases already pin.
"""

from __future__ import annotations

import pytest

from reckon.crew import review_need
from reckon.crew.picker import client
from tests.test_review_is_earned_by_significant_change import (
    DONE_WHEN,
    PLAIN_A,
    PLAIN_B,
    _coverage,
    _review,
)
from tests.test_review_is_earned_by_significant_change import (
    project as project,  # noqa: PLC0414
)
from tests.test_review_is_earned_by_significant_change import (
    sectioned as sectioned,  # noqa: PLC0414
)


class StubJev:
    """Answer every Noul question with one probability and record each request."""

    def __init__(self, probability: float) -> None:
        self.probability = probability
        self.requests: list[tuple[dict, dict]] = []

    def __call__(self, state, questions, *, env_path):
        self.requests.append((state, questions))
        return {
            "model": "stub",
            "answers": {
                key: {"type": "noul", "noul": self.probability} for key in questions
            },
        }


@pytest.fixture
def judge_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    return tmp_path / "config"


def _change(identity: str, reviewed: str, present: str) -> review_need.Change:
    return review_need.Change(identity=identity, reviewed=reviewed, present=present)


# ── The judge ───────────────────────────────────────────────────────────────


def test_every_change_is_asked_in_one_request_and_read_back_from_the_cache(
    judge_home,
):
    stub = StubJev(0.9)
    changes = [
        _change("s1", "Ship the parser.", "Ship the parser and its tests."),
        _change("s2", "Keep one record.", "Keep one record per run."),
    ]

    first = review_need.judge(
        changes, subject="plan", goals={"plan": "P"}, threshold=0.5, caller=stub
    )
    again = review_need.judge(
        changes, subject="plan", goals={"plan": "P"}, threshold=0.5, caller=stub
    )

    assert len(stub.requests) == 1
    state, questions = stub.requests[0]
    assert len(questions) == 2
    assert all(question["type"] == "noul" for question in questions.values())
    assert state["changes"][0]["diff"].startswith("--- reviewed")
    assert state["goals"] == {"plan": "P"}
    assert state["review_checks"]
    assert {verdict.source for verdict in first.values()} == {"jev"}
    assert {verdict.source for verdict in again.values()} == {"cache"}
    assert all(verdict.required for verdict in again.values())


def test_the_threshold_turns_the_probability_into_the_decision(judge_home):
    change = [_change("s1", "Ship it.", "Ship it now.")]

    low = review_need.judge(
        change, subject="run", goals={}, threshold=0.5, caller=StubJev(0.2)
    )["s1"]

    assert low.required is False
    assert low.probability == pytest.approx(0.2)


def test_an_unchanged_unit_is_not_asked_about(judge_home):
    stub = StubJev(0.9)

    verdicts = review_need.judge(
        [_change("s1", "Same text.", "Same text.")],
        subject="plan",
        goals={},
        threshold=0.5,
        caller=stub,
    )

    assert stub.requests == []
    assert verdicts["s1"].required is False


@pytest.mark.parametrize(
    "caller,reason",
    [
        (
            lambda *a, **k: (_ for _ in ()).throw(TimeoutError("x")),
            "jev-error: TimeoutError",
        ),
        (lambda *a, **k: {"answers": {"change_0": {"noul": 7}}}, "not a probability"),
    ],
    ids=["failing-service", "malformed-answer"],
)
def test_a_judge_that_cannot_answer_leaves_the_decision_to_the_caller(
    judge_home, caller, reason
):
    verdict = review_need.judge(
        [_change("s1", "Ship it.", "Ship it now.")],
        subject="plan",
        goals={},
        threshold=0.5,
        caller=caller,
    )["s1"]

    assert verdict.required is None
    assert reason in verdict.reason


def test_a_diff_too_large_to_read_whole_is_left_to_the_caller(judge_home):
    stub = StubJev(0.0)
    huge = ". ".join(f"sentence {index}" for index in range(5000))

    verdict = review_need.judge(
        [_change("s1", "Short.", huge)],
        subject="plan",
        goals={},
        threshold=0.5,
        caller=stub,
    )["s1"]

    assert stub.requests == []
    assert verdict.required is None


# ── The plan's coverage reads the judge ─────────────────────────────────────


def _reword(text: str, share: float) -> str:
    words = text.split()
    count = round(len(words) * share)
    return " ".join([f"rewritten{index:02d}" for index in range(count)] + words[count:])


def test_a_large_rewording_judged_immaterial_stays_covered(sectioned, monkeypatch):
    _, _, path = sectioned
    _review(path)
    path.write_text(path.read_text().replace(PLAIN_B, _reword(PLAIN_B, 0.6)))
    monkeypatch.setattr(client, "ask", StubJev(0.1))

    _records, uncovered, _changes = _coverage(path)

    assert "b" not in uncovered


def test_a_small_change_judged_material_is_uncovered(sectioned, monkeypatch):
    _, _, path = sectioned
    _review(path)
    path.write_text(path.read_text().replace(PLAIN_A, _reword(PLAIN_A, 0.04)))
    monkeypatch.setattr(client, "ask", StubJev(0.9))

    _records, uncovered, _changes = _coverage(path)

    assert "a" in uncovered


def test_a_done_when_rewording_is_judged_rather_than_always_uncovered(
    sectioned, monkeypatch
):
    _, _, path = sectioned
    _review(path)
    path.write_text(
        path.read_text().replace(DONE_WHEN, "charlie ships with nothing regressing")
    )
    stub = StubJev(0.1)
    monkeypatch.setattr(client, "ask", stub)

    _records, uncovered, _changes = _coverage(path)

    assert "c" not in uncovered
    state, _questions = stub.requests[0]
    assert state["goals"]["plan"] == "Fixture"
    assert "charlie ships with nothing regressing" in state["changes"][0]["goal"]


def test_an_unanswered_judge_falls_back_to_the_word_share(sectioned):
    _, _, path = sectioned
    _review(path)
    path.write_text(path.read_text().replace(PLAIN_A, _reword(PLAIN_A, 0.04)))
    path.write_text(path.read_text().replace(PLAIN_B, _reword(PLAIN_B, 0.6)))

    _records, uncovered, changes = _coverage(path)

    assert "a" not in uncovered
    assert "b" in uncovered
    assert changes["b"] > 0.30
