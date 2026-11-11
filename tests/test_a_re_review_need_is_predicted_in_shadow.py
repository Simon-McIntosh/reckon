"""A further code review's predicted outcome is recorded beside what it found.

No re-review is skipped on the prediction: every one is dispatched, and the row
exists so that a decision to skip some can be taken on measured rows. The rows
therefore have to carry three things and nothing may stand in for them — the
probability, the inputs it was judged from, and, once the review is stored, what
the review actually found. A first review is not judged at all, because a row
for one would put a guess about no suspicion into the population the threshold
counts are read from.

Everything here runs against a temporary crew home and a stubbed Jev, so nothing
reaches the live service and no real run's review state is touched.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reckon.crew import recovery_review_dispatch, review, review_need
from reckon.crew.picker import client

PROJECT = "probe"
RUN_ID = "r-probed-run"
REVIEW_RUN_ID = "r-probed-review"


class StubJev:
    """Answer every Noul question with one probability, and record the request."""

    def __init__(self, probability: float = 0.8) -> None:
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
def crew_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(home))
    return home


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Reckon Test",
            "-c",
            "user.email=test@example.invalid",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def reviewed_run(tmp_path) -> dict:
    """A repository holding one run reviewed at the first of its two commits."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "work.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "work.py")
    _git(repo, "commit", "-qm", "the reviewed revision")
    reviewed_head = _git(repo, "rev-parse", "HEAD")
    (repo / "work.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "work.py")
    _git(repo, "commit", "-qm", "the change since the review")
    head = _git(repo, "rev-parse", "HEAD")
    return {
        "repo": repo,
        "reviewed_head": reviewed_head,
        "head": head,
        "record": {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repo),
            "worktree": str(repo),
            "subject": "run",
            "node": {
                "id": "probed-node",
                "goal": "the work stores the value it read",
                "done_when": "a test shows the stored value",
            },
        },
    }


def _store_prior_review(
    reviewed_run: dict, *, findings: list[dict] | None = None
) -> None:
    review.store_review(
        {
            "project": PROJECT,
            "reviewed_run_id": RUN_ID,
            "review_run_id": "r-the-first-review",
            "status": "parsed",
            "scores": {
                "correctness": 4,
                "design": 4,
                "tests": 4,
                "clarity": 4,
                "scope": 4,
                "safety": 4,
            },
            "findings": findings
            if findings is not None
            else [{"text": "the helper is unnamed", "severity": "non-blocking"}],
            "reviewed_base_sha": reviewed_run["reviewed_head"],
            "reviewed_head_sha": reviewed_run["reviewed_head"],
        }
    )


def _fields(reviewed_run: dict) -> dict:
    return {
        "project": PROJECT,
        "head": reviewed_run["head"],
        "run_id": RUN_ID,
        "from_run": RUN_ID,
        "node_id": "probed-node",
    }


def _stub(monkeypatch, stub: StubJev | None) -> StubJev:
    replacement = stub or StubJev()
    monkeypatch.setattr(client, "ask", replacement)
    return replacement


# ── First reviews are not judged ────────────────────────────────────────────


def test_a_first_review_carries_no_prediction(crew_home, reviewed_run, monkeypatch):
    _stub(monkeypatch, None)

    written = recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )

    assert written is None
    assert review_need.recorded_rows(PROJECT) == []


# ── A further review's row carries the judgement and its inputs ─────────────


def test_a_second_review_carries_the_prediction_and_its_inputs(
    crew_home, reviewed_run, monkeypatch
):
    _store_prior_review(reviewed_run)
    stub = _stub(monkeypatch, StubJev(probability=0.73))

    written = recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )

    assert written is not None
    rows = review_need.recorded_rows(PROJECT)
    assert len(rows) == 1
    row = rows[0]
    assert row["review_run_id"] == REVIEW_RUN_ID
    assert row["reviewed_run_id"] == RUN_ID
    assert row["available"] is True
    assert row["probability"] == pytest.approx(0.73)
    inputs = row["inputs"]
    assert inputs["goal"] == "the work stores the value it read"
    assert inputs["done_when"] == "a test shows the stored value"
    assert inputs["previous_reviewed_head"] == reviewed_run["reviewed_head"]
    assert inputs["head"] == reviewed_run["head"]
    assert inputs["paths"] == ["work.py"]
    assert inputs["diff_chars"] > 0
    assert inputs["prior_review"]["blocking_findings"] == 0
    # The question was asked of the re-review subject, not of the plan-edit one.
    state = stub.requests[0][0]
    assert state["subject"] == review_need.REREVIEW_SUBJECT


# ── The stored review run's own record still carries the row after promotion ─


def test_the_row_survives_and_is_keyed_by_the_review_run(
    crew_home, reviewed_run, monkeypatch
):
    _store_prior_review(reviewed_run)
    _stub(monkeypatch, StubJev(0.4))

    recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )

    path = review_need._shadow_path(PROJECT, REVIEW_RUN_ID)
    assert path.is_file()
    assert path.parent.parent == crew_home / "crew" / review_need.SHADOW_ROOT_NAME


# ── The outcome is recorded beside the prediction ───────────────────────────


def test_the_stored_review_records_its_outcome_beside_the_prediction(
    crew_home, reviewed_run, monkeypatch
):
    _store_prior_review(reviewed_run)
    _stub(monkeypatch, StubJev(probability=0.9))
    recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )
    pointer = {
        **reviewed_run["record"],
        "review_dispatch": {"run_id": REVIEW_RUN_ID},
    }

    recovery_review_dispatch._record_shadow_outcome(
        pointer,
        {
            "review_run_id": REVIEW_RUN_ID,
            "findings": [{"text": "the value is never stored", "severity": "blocking"}],
        },
    )

    row = review_need.recorded_rows(PROJECT)[0]
    assert row["outcome"]["verdict"] == "blocking"
    assert row["outcome"]["blocking"] == 1
    assert row["probability"] == pytest.approx(0.9)


def test_a_clean_review_records_a_clean_outcome(crew_home, reviewed_run, monkeypatch):
    _store_prior_review(reviewed_run)
    _stub(monkeypatch, StubJev(probability=0.1))
    recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )
    pointer = {**reviewed_run["record"], "review_dispatch": {"run_id": REVIEW_RUN_ID}}

    recovery_review_dispatch._record_shadow_outcome(
        pointer, {"review_run_id": REVIEW_RUN_ID, "findings": []}
    )

    assert review_need.recorded_rows(PROJECT)[0]["outcome"]["verdict"] == "clean"


# ── An unavailable judge still dispatches the review ────────────────────────


def test_an_unavailable_jev_still_dispatches_and_records_unavailable(
    crew_home, reviewed_run, monkeypatch
):
    _store_prior_review(reviewed_run)

    def refuse(state, questions, *, env_path):
        raise OSError("the decisions endpoint is unreachable")

    monkeypatch.setattr(client, "ask", refuse)

    written = recovery_review_dispatch.record_re_review_prediction(
        reviewed_run["record"], _fields(reviewed_run), REVIEW_RUN_ID
    )

    assert written is not None
    row = review_need.recorded_rows(PROJECT)[0]
    assert row["available"] is False
    assert row["probability"] is None
    assert row["reason"].startswith("jev-error")
    assert review_need.shadow_view(PROJECT)["totals"]["unavailable"] == 1


# ── The view counts what each threshold would have skipped ──────────────────


def _plant(project: str, index: int, probability: float, outcome: str) -> None:
    verdict = review_need.Verdict(True, probability, "jev")
    review_need.record_prediction(
        project=project,
        review_run_id=f"r-planted-{index}",
        reviewed_run_id=f"r-run-{index}",
        verdict=verdict,
        inputs={"index": index},
    )
    review_need.record_outcome(
        project=project, review_run_id=f"r-planted-{index}", outcome=outcome
    )


def test_the_view_counts_what_two_thresholds_would_have_skipped(crew_home):
    _plant(PROJECT, 1, 0.10, "blocking")
    _plant(PROJECT, 2, 0.20, "clean")
    _plant(PROJECT, 3, 0.40, "blocking")
    _plant(PROJECT, 4, 0.90, "clean")

    view = review_need.shadow_view(PROJECT, thresholds=(0.25, 0.5))

    low = view["thresholds"]["0.25"]
    assert low["skipped"] == 2
    assert low["skipped_blocking"] == 1
    assert low["skipped_clean"] == 1
    high = view["thresholds"]["0.50"]
    assert high["skipped"] == 3
    assert high["skipped_blocking"] == 2
    assert high["skipped_clean"] == 1
    assert high["kept"] == 1
    assert view["totals"]["blocking"] == 2
    assert view["totals"]["clean"] == 2
    assert view["rows_needed"] == review_need.SHADOW_ROWS_FOR_DECISION - 4


def test_an_unmeasured_row_is_counted_as_pending_never_as_clean(crew_home):
    _plant(PROJECT, 1, 0.1, "blocking")
    review_need.record_prediction(
        project=PROJECT,
        review_run_id="r-planted-2",
        reviewed_run_id="r-run-2",
        verdict=review_need.Verdict(True, 0.1, "jev"),
        inputs={},
    )

    view = review_need.shadow_view(PROJECT, thresholds=(0.5,))

    assert view["thresholds"]["0.50"]["skipped_clean"] == 0
    assert view["thresholds"]["0.50"]["skipped_pending"] == 1
    assert view["totals"]["awaiting_outcome"] == 1
