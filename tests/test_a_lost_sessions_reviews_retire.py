# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

"""A lost session's reviews retire, and none is composed under it.

A session nothing has heard for the flight threshold is lost. Its queued or
awaiting-upstream reviews can never be read, so the admit pass retires them and
records the departure with the session, the instant it was last heard and the
rule. A finished run it owns that owes a review gets none composed by the reflex
or by recovery, because a composed review would only be refused.

Every case here is paired with the same fixture made live: the live session's
records are untouched, the pass retires nothing, and the reflex composes the
review it owes. That pairing is the declared negative control -- a rule that
fires on a live session would pass the retirement case while being wrong.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import promotion, recovery_watch, runs
from reckon.crew.admission import verdicts as _verdicts
from reckon.crew.follower_registration import renew_follower_lease
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    SESSION,
    _config_for,
    _launcher,
    _queue,
    admit_repo,
)

HOUR = 3600.0
LOST_SESSION = "lost-coordinator"
LIVE_SESSION = "live-coordinator"


def _age_pointer(run_id: str, *, session: str, age_hours: float) -> dict:
    """Rewrite a queued request's own clock, so its session ages by it.

    A session that never registered a follower is dated from its oldest live
    record, so moving that record's creation moves the session's last-heard
    instant with no lease and no process to fabricate.
    """

    stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()

    def rewrite(current: dict) -> dict:
        current["session"] = session
        current["created_at"] = stamp
        current["queued_at"] = stamp
        return current

    runs._mutate_pointer(run_id, rewrite)
    return runs.read_pointer(run_id)


def _queued_review(repository: Path, *, run_id: str, session: str) -> dict:
    """A queued review request, standing in for one a session left behind."""

    brief_dir = runs.run_dir(run_id)
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief = brief_dir / "brief.md"
    brief.write_text("review the delivered run\n", encoding="utf-8")
    request = _queue(repository, node_id=run_id, run_id=run_id, session=session)
    request["node"]["role"] = "review"
    request["node"]["brief"] = str(brief)
    request["node"]["brief_path"] = str(brief)
    runs.queue_dispatch(request)
    return runs.read_pointer(run_id)


def _discard_marker(run_id: str) -> dict:
    """The discard record a retirement left, wherever the discard put it."""

    for path in (
        promotion.discard_record_path(run_id),
        promotion.discard_spool_path(run_id),
    ):
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise AssertionError(f"no discard record was written for {run_id!r}")


def _admit(config_for, launcher, *, run_id: str = ""):
    from reckon.crew import queue_admission

    return queue_admission.admit_once(
        config_for=config_for, launcher=launcher, run_id=run_id
    )


# --------------------------------------------------------------------------
# The admit pass retires a lost session's queued review.
# --------------------------------------------------------------------------


def test_a_lost_sessions_queued_review_is_retired_with_its_facts(
    admit_repo: Path,
) -> None:
    """One pass retires it and records the session, the instant and the rule."""

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-lost-review"
    _queued_review(admit_repo, run_id=run_id, session=LOST_SESSION)
    aged = _age_pointer(run_id, session=LOST_SESSION, age_hours=73)
    recorded: list = []

    result = _admit(resolve, _launcher(recorded))

    assert result["retired_lost_session"] == [run_id]
    assert not runs.pointer_path(run_id).exists(), (
        "a retired review leaves no live request"
    )
    marker = _discard_marker(run_id)
    assert marker["lost_session"] == LOST_SESSION
    assert marker["last_heard_at"] == aged["created_at"]
    assert "lost_session.after_hours" in marker["rule"], marker["rule"]


def test_a_lost_sessions_awaiting_upstream_review_is_retired_too(
    admit_repo: Path,
) -> None:
    """A review waiting on an upstream is retired on the pass that finds it.

    A request parked against an upstream will be released only when that
    upstream lands, and a lost session is retired without waiting for it: the
    session is gone whenever the upstream arrives.
    """

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-lost-review-waiting"
    _queued_review(admit_repo, run_id=run_id, session=LOST_SESSION)
    upstream = _queued_review(
        admit_repo, run_id="r-upstream-still-queued", session=LIVE_SESSION
    )
    _age_pointer(run_id, session=LOST_SESSION, age_hours=73)

    def park(current: dict) -> dict:
        current["phase"] = "awaiting-upstream"
        current["refs"] = [f"run:{upstream['run_id']}"]
        return current

    runs._mutate_pointer(run_id, park)
    assert runs.read_pointer(run_id)["phase"] == "awaiting-upstream"

    result = _admit(resolve, _launcher([]))

    assert result["retired_lost_session"] == [run_id]
    assert not runs.pointer_path(run_id).exists()
    assert runs.pointer_path("r-upstream-still-queued").exists()


def test_a_record_dated_by_an_epoch_still_dates_its_session(
    admit_repo: Path,
) -> None:
    """A pointer whose creation stamp is an epoch reads lost, not undated.

    Some writers -- older dispatches and most fixtures -- store the creation
    instant as a number. A reader that stringifies it before parsing reads no
    date at all, so the session looks heard just now and is never retired.
    """

    from reckon.crew import lost_sessions

    made = datetime.now(UTC) - timedelta(hours=73)
    pointer = {"created_at": made.timestamp(), "project": PROJECT}

    answer = lost_sessions.session_lost(PROJECT, "epoch-session", pointers=[pointer])

    assert answer["last_heard_at"] is not None, "an epoch dates the session"
    assert answer["lost"] is True, answer


def test_a_second_pass_creates_no_run_and_attempts_no_launch(
    admit_repo: Path,
) -> None:
    """The retirement is once: a later pass has nothing to attempt."""

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-lost-review-twice"
    _queued_review(admit_repo, run_id=run_id, session=LOST_SESSION)
    _age_pointer(run_id, session=LOST_SESSION, age_hours=73)

    first = _admit(resolve, _launcher([]))
    recorded: list = []
    second = _admit(resolve, _launcher(recorded))

    assert first["retired_lost_session"] == [run_id]
    assert second["retired_lost_session"] == []
    assert second["launched"] == []
    assert recorded == [], "no launch is attempted for a retired request"
    assert not runs.pointer_path(run_id).exists()


def test_a_live_sessions_queued_review_is_untouched(admit_repo: Path) -> None:
    """The identical record under a live session is neither retired nor held back
    by the rule: the pass launches it."""

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-live-review"
    _queued_review(admit_repo, run_id=run_id, session=LIVE_SESSION)
    _age_pointer(run_id, session=LIVE_SESSION, age_hours=1)
    recorded: list = []

    result = _admit(resolve, _launcher(recorded))

    assert result["retired_lost_session"] == []
    assert not promotion.discard_recorded(run_id)
    assert runs.pointer_path(run_id).exists(), "a live session's review stays"
    assert run_id in result["launched"], result
    assert recorded, "the live session's review is launched"


def test_the_verdict_record_lists_each_retirement_under_its_key(
    admit_repo: Path,
) -> None:
    """The pass's verdict record names every retirement it made."""

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-lost-review-verdict"
    _queued_review(admit_repo, run_id=run_id, session=LOST_SESSION)
    _age_pointer(run_id, session=LOST_SESSION, age_hours=73)

    result = _admit(resolve, _launcher([]))
    record = _verdicts._pass_verdict_record(result, lane="", code_stamp="")

    assert record is not None, "a pass that retired a request writes a verdict line"
    assert record["retired_lost_session"] == [run_id]
    retired_rows = [
        row for row in record["pointers"] if row["verdict"] == "retired-lost-session"
    ]
    assert [row["run_id"] for row in retired_rows] == [run_id]


def test_a_named_admission_retires_only_the_request_it_was_handed(
    admit_repo: Path,
) -> None:
    """A pass asked about one run leaves every other request alone.

    A named admission decides only the request it was given. The lost-session
    rule ranges over that same scope: a fleet-wide sweep here would retire a
    stranger's review on a pass whose caller was promised the rest of the queue
    would not be touched.
    """

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    named = "r-lost-review-named"
    other = "r-lost-review-unnamed"
    _queued_review(admit_repo, run_id=named, session=LOST_SESSION)
    _queued_review(admit_repo, run_id=other, session=LOST_SESSION)
    _age_pointer(named, session=LOST_SESSION, age_hours=73)
    _age_pointer(other, session=LOST_SESSION, age_hours=73)
    recorded: list = []

    named_pass = _admit(resolve, _launcher(recorded), run_id=named)

    assert named_pass["retired_lost_session"] == [named]
    assert not runs.pointer_path(named).exists()
    assert runs.pointer_path(other).exists(), "the other request is untouched"
    assert not promotion.discard_recorded(other)
    assert recorded == [], "a retired request attempts no launch"

    fleet_pass = _admit(resolve, _launcher([]))

    assert fleet_pass["retired_lost_session"] == [other]


def test_a_refused_discard_is_not_reported_as_a_retirement(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discard that refuses leaves the request queued and says so.

    The pass names the refusal in its obligation and stamps it on the pointer,
    so the next pass retries; reporting the request retired would tell the
    caller it left the fleet when it did not.
    """

    from reckon.crew import promotion as promotion_module
    from reckon.crew.node import CrewError

    config, resolve = _config_for()
    config["roles"]["review"] = {}
    run_id = "r-lost-review-refused"
    _queued_review(admit_repo, run_id=run_id, session=LOST_SESSION)
    _age_pointer(run_id, session=LOST_SESSION, age_hours=73)

    def refuse(_run_id: str) -> None:
        raise CrewError("the discard lock is held")

    monkeypatch.setattr(promotion_module, "discard", refuse)

    result = _admit(resolve, _launcher([]))

    assert result["retired_lost_session"] == []
    assert result["retirement_refused"] == [run_id]
    retired_rows = [
        row for row in result["verdicts"] if row["verdict"] == "retired-lost-session"
    ]
    assert retired_rows == [], "no verdict row claims a retirement that did not happen"
    assert result["launched"] == [], (
        "a request that could not be retired is not launched either"
    )
    assert runs.pointer_path(run_id).is_file(), "the request is still queued"
    stamped = runs.read_pointer(run_id)
    assert "pass_hold" in stamped, stamped
    hold = stamped["pass_hold"]
    assert hold["verdict"] == "retired-lost-session"
    assert "refused" in hold["reason"]
    assert any(run for run in result["obligations"] if run.get("run_id") == run_id), (
        result["obligations"]
    )


# --------------------------------------------------------------------------
# The reflex composes no review for a finished run of a lost session.
# --------------------------------------------------------------------------


@pytest.fixture
def scoring_fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A recovery sweep whose one finished run is owed a review.

    Classification and the dispatch itself are stood in for so the case measures
    the sweep's own decision, not the dispatch machinery behind it. The
    lost-session derivation and the guard are the real code under test.
    """

    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    session = LOST_SESSION
    made = datetime.now(UTC) - timedelta(hours=73)
    pointers = [
        {
            "run_id": "r-delivered",
            "project": "named",
            "session": session,
            "created_at": made.timestamp(),
        }
    ]
    monkeypatch.setattr(recovery_watch, "list_live", lambda: pointers)
    monkeypatch.setattr(
        importlib.import_module("reckon.crew.dispatch"),
        "observe",
        lambda run_id, config=None: next(p for p in pointers if p["run_id"] == run_id),
    )
    monkeypatch.setattr(
        recovery_watch, "_derive_missing_manifest", lambda p, config=None: p
    )
    monkeypatch.setattr(
        recovery_watch,
        "classify_pointer",
        lambda p: {"run_id": p["run_id"], "classification": "scoring"},
    )
    launches: list[str] = []

    def compose(record, **_kwargs):
        launches.append(str(record["run_id"]))
        return {
            "run_id": record["run_id"],
            "dispatched": True,
            "review_run_id": "review-of-" + str(record["run_id"]),
        }

    monkeypatch.setattr(recovery_watch, "dispatch_review_for_run", compose)
    return pointers, launches


def test_a_finished_run_of_a_lost_session_composes_no_review(
    scoring_fleet,
) -> None:
    """Three reflex passes compose zero reviews for a lost session's run."""

    pointers, launches = scoring_fleet
    reports = [
        recovery_watch.recover(project="named", dispatch_reviews=True, any_session=True)
        for _ in range(3)
    ]

    assert launches == [], "no review is composed under a lost session"
    for report in reports:
        assert report["reviews_dispatched"] == []
        assert [row["run_id"] for row in report["reviews_awaiting_lost_session"]] == [
            "r-delivered"
        ]
    assert pointers[0]["run_id"] == "r-delivered", "the run is still carried"


def test_a_finished_run_of_a_live_session_still_composes_its_review(
    scoring_fleet, tmp_path: Path
) -> None:
    """The negative control: make the session live and the reflex composes.

    The identical record under a live session must yield the review the lost
    case suppresses. Without this, a sweep that composed nothing at all would
    pass the case above.
    """

    pointers, launches = scoring_fleet
    pointers[0]["session"] = LIVE_SESSION
    renew_follower_lease("named", LIVE_SESSION)

    reports = [
        recovery_watch.recover(project="named", dispatch_reviews=True, any_session=True)
        for _ in range(3)
    ]

    assert launches == ["r-delivered"] * 3, "the live session's review is composed"
    for report in reports:
        assert report["reviews_dispatched"] == ["review-of-r-delivered"]
        assert report["reviews_awaiting_lost_session"] == []


def test_a_sessions_lostness_is_read_from_its_oldest_record(
    scoring_fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh run does not make its lost session look heard.

    The run that owes the review may itself be young while its session has
    another live record nothing has touched for longer than the threshold.
    Dating the session from the scoring run alone reports it live, and the
    review is composed only to be refused -- so every live record dates it.
    """

    pointers, launches = scoring_fleet
    fresh = datetime.now(UTC) - timedelta(hours=1)
    pointers[0]["created_at"] = fresh.timestamp()
    pointers.append(
        {
            "run_id": "r-stale-sibling",
            "project": "named",
            "session": LOST_SESSION,
            "created_at": (datetime.now(UTC) - timedelta(hours=73)).timestamp(),
        }
    )
    monkeypatch.setattr(
        recovery_watch,
        "classify_pointer",
        lambda p: {
            "run_id": p["run_id"],
            "classification": "scoring" if p["run_id"] == "r-delivered" else "held",
        },
    )

    report = recovery_watch.recover(
        project="named", dispatch_reviews=True, any_session=True
    )

    assert launches == [], "the session is lost; no review is composed"
    assert [row["run_id"] for row in report["reviews_awaiting_lost_session"]] == [
        "r-delivered"
    ]
