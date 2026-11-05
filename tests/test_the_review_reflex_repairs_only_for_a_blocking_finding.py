"""A repair waits for coordinator input and ignores reviews with only follow-ons."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon.crew import recovery_repair_dispatch as reflex
from reckon.crew import repair, review_dispositions, runs
from reckon.crew import review as review_module
from tests import test_repair_answers_only_blocking_findings as prior

dispatch_project = prior.dispatch_project


def _stored_review(head: str, findings: list[dict], *, durability: int = 15) -> None:
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 15)
    scores["durability"] = durability
    review_module.store_review(
        {
            "project": prior.PROJECT,
            "reviewed_run_id": prior.RUN_ID,
            "review_run_id": "r-independent-review",
            "reviewed_base_sha": head,
            "reviewed_head_sha": head,
            "status": "parsed",
            "scores": scores,
            "total": sum(scores.values()),
            "findings": findings,
        }
    )


def _reviewed_pointer(home: Path, repo: Path) -> dict:
    pointer = prior._reviewed_pointer(home, repo)
    pointer["review_dispatch"] = {
        "status": "dispatched",
        "run_id": "r-independent-review",
    }
    runs._write_json(runs.pointer_path(prior.RUN_ID), pointer)
    return pointer


@pytest.mark.arms_watch_producer
def test_follow_ons_do_not_open_a_repair_and_blocking_advice_waits(
    dispatch_project: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, head = dispatch_project
    pointer = _reviewed_pointer(home, repo)
    _stored_review(head, [prior.FOLLOW_ON])
    resumed = prior._stub_resume(monkeypatch)

    follow_on, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert not follow_on.get("resumed")
    assert not follow_on.get("awaiting_coordinator")
    assert resumed == []

    _stored_review(head, [prior.BLOCKING])
    clock = [1000.0]
    monkeypatch.setattr(reflex, "time", SimpleNamespace(time=lambda: clock[0]))
    held, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert held.get("awaiting_coordinator") is True, held
    assert held["hold_until"] == 1060.0
    assert resumed == []

    advice_path = Path(held["advice_path"])
    assert advice_path.is_relative_to(home)
    advice_path.parent.mkdir(parents=True, exist_ok=True)
    advice_path.write_text(
        "Also repair the boundary case I observed.\n", encoding="utf-8"
    )
    clock[0] = held["hold_until"] - 1
    waiting, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert waiting["awaiting_coordinator"] is True
    assert resumed == []

    clock[0] = held["hold_until"] + 1
    launched, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert launched["resumed"] is True
    assert len(resumed) == 1
    assert prior._expected_id(prior.BLOCKING) in resumed[0]["advice"]
    assert "Also repair the boundary case I observed." in resumed[0]["advice"]


@pytest.mark.arms_watch_producer
def test_unanswered_sub_floor_dimension_opens_the_same_hold(
    dispatch_project: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, head = dispatch_project
    pointer = _reviewed_pointer(home, repo)
    pointer["node"]["write_paths"] = ["pkg/mod.py"]
    runs._write_json(runs.pointer_path(prior.RUN_ID), pointer)
    _stored_review(head, [], durability=9)
    config = {**prior.CONFIG, "gates": {"dimension_floors": {"durability": 12}}}
    resumed = prior._stub_resume(monkeypatch)
    clock = [2000.0]
    monkeypatch.setattr(reflex, "time", SimpleNamespace(time=lambda: clock[0]))

    with runs.follower_claim(prior.PROJECT, "session-orchestrating", delivery="stream"):
        held = reflex.dispatch_repair_for_run(
            pointer, config=config, launcher=lambda *a, **k: 1
        )
    assert held["awaiting_coordinator"] is True
    assert resumed == []
    durable = runs.read_pointer(prior.RUN_ID)[reflex.REPAIR_ADVICE_HOLD_FIELD]
    assert durable["advice_path"] == held["advice_path"]
    assert durable["until"] == held["hold_until"]
    recorded = runs.read_pointer(prior.RUN_ID)["repair_dispatch"]
    assert recorded["status"] == "advice-hold"
    assert held["advice_path"] in recorded["reason"]

    clock[0] = held["hold_until"] + 1
    with runs.follower_claim(prior.PROJECT, "session-orchestrating", delivery="stream"):
        launched = reflex.dispatch_repair_for_run(
            pointer, config=config, launcher=lambda *a, **k: 1
        )
    assert launched["resumed"] is True
    assert "dimension-durability" in resumed[0]["advice"]
    assert "scored 9 below its declared floor 12" in resumed[0]["advice"]

    source = repo / "pkg" / "mod.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "pkg/mod.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-q",
            "-m",
            "fix: answer review",
            "-m",
            "Move the reviewed source head.",
        ],
        cwd=repo,
        check=True,
    )
    with runs.follower_claim(prior.PROJECT, "session-orchestrating", delivery="stream"):
        reflex.dispatch_repair_for_run(
            pointer, config=config, launcher=lambda *a, **k: 1
        )
    stored = review_module.read_review(
        prior.PROJECT, prior.RUN_ID, reviewed_head_sha=head
    )
    assert review_dispositions.sub_floor_dimensions(stored, {"durability": 12}) == []


@pytest.mark.arms_watch_producer
def test_without_the_blocking_filter_follow_ons_start_a_round(
    dispatch_project: tuple[Path, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, repo, head = dispatch_project
    pointer = _reviewed_pointer(home, repo)
    _stored_review(head, [prior.FOLLOW_ON])
    resumed = prior._stub_resume(monkeypatch)
    monkeypatch.setattr(repair, "blocking_findings", repair.review_findings)
    clock = [3000.0]
    monkeypatch.setattr(reflex, "time", SimpleNamespace(time=lambda: clock[0]))

    held, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert held["awaiting_coordinator"] is True
    clock[0] = held["hold_until"] + 1
    launched, _ = prior._dispatch_repair(pointer, monkeypatch)
    assert launched["resumed"] is True
    assert prior._expected_id(prior.FOLLOW_ON) in resumed[0]["advice"]
