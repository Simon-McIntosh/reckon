"""A plan's review composes itself after its author's burst, and only then.

A coordinator must remember to compose a content review after editing a plan,
and a coordinator paused at a rate limit cannot. The producer pass — the crew
watch sweep — composes the review a plan owes once it has been left alone for
the quiet period, composes nothing for a plan still being authored, for one
whose edit is no longer an authoring burst, for one no recent run names and for
one already under review, and answers the not-changing findings of the reviews
it composed itself so the coordinator is woken only for the ones that would.

The scope is the whole point of this file. The first landing composed for every
plan owing a review, so it reviewed documents nobody was editing, under
whichever session ran the sweep, and answered findings in stored reviews the
pass had not written. Each arm below fails if one of those three returns.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import (
    plan_review,
    recovery,
    recovery_review_dispatch,
    resumption,
    runs,
)
from reckon.crew import review as review_store
from tests.test_review_plan_command import CONFIG
from tests.test_review_plan_command import project as project  # noqa: PLC0414

QUIET_EDIT_SECONDS = 11 * 60
FRESH_EDIT_SECONDS = 2 * 60
# A plan whose last edit is this old is past the burst window, so it owes a
# review its coordinator must compose and the reflex must leave alone.
STALE_EDIT_SECONDS = 2 * 24 * 60 * 60
# How long before now the run that names the plan was dispatched. It is well
# inside the day the reflex requires, so an arm whose plan is refused is
# refused for its edit's age rather than for a missing run.
AUTHOR_RUN_SECONDS = 60 * 60
STALE_RUN_SECONDS = 2 * 24 * 60 * 60
AUTHOR_SESSION = "plan-author"


def _edit(path, seconds_ago: float) -> None:
    """Give the plan the modification time an edit that many seconds ago leaves."""
    stamp = time.time() - seconds_ago
    os.utime(path, (stamp, stamp))


def _run_naming(
    plan_slug: str = "fixture",
    *,
    session: str = AUTHOR_SESSION,
    seconds_ago: float = AUTHOR_RUN_SECONDS,
    run_id: str | None = None,
) -> str:
    """Write the live pointer a crew run on the plan leaves, and return its id."""
    name = run_id or f"r-{plan_slug}-authoring"
    moment = (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()
    directory = runs.live_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "run_id": name,
                "project": "sample",
                "session": session,
                "phase": "working",
                "created_at": moment,
                "launched_at": moment,
                "node": {"id": f"build-{plan_slug}", "plan": plan_slug},
            }
        ),
        encoding="utf-8",
    )
    return name


SUBJECTS: list[dict] = []


def _launch(monkeypatch) -> list[str]:
    """Stand in for the launcher, so a pass dispatches without spawning."""
    dispatch = importlib.import_module("reckon.crew.dispatch")
    launched: list[str] = []
    subjects = SUBJECTS

    def dispatch_instead(**kwargs):
        launched.append(str(kwargs.get("session") or ""))
        return {"run_id": "composed-review"}

    subjects.clear()

    composed_subject = recovery.plan_review_subject

    def subject_instead(*args, **kwargs):
        subjects.append(dict(kwargs))
        return composed_subject(*args, **kwargs)

    monkeypatch.setattr(recovery, "plan_review_subject", subject_instead)
    monkeypatch.setattr(dispatch, "dispatch", dispatch_instead)
    return launched


def _pass(project_name: str = "sample") -> dict:
    """Run one producer pass and return what it composed for plans."""
    return plan_review.compose_owed_plan_reviews(
        project_name, config=CONFIG, launcher=None
    )


def _sweep_pass(project_name: str = "sample") -> dict:
    """Run the crew watch sweep, which is where the pass is wired in."""
    return (
        resumption.sweep(project_name, session="sweeper", config=CONFIG).get(
            "plan_reviews"
        )
        or {}
    )


def _run_directories():
    root = plan_review.review_report_directory("sample", "fixture", "unused").parent
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _stored_review(**kwargs) -> Path:
    record = {
        "project": "sample",
        "plan_slug": "fixture",
        "plan_version": 3,
        "rubric": "content",
        "findings": [],
        **kwargs,
    }
    return plan_review.store_plan_review(record)


def _composed_by_the_reflex(*run_ids: str, plan_slug: str = "fixture") -> None:
    """Record that this reflex dispatched these reviews, as a pass would."""
    plan_review._record_weighed_edits("sample", {}, composed={plan_slug: list(run_ids)})


def _two_findings() -> list[dict]:
    return [
        {
            "id": "scope-1",
            "type": "scope",
            "text": "The plan names a file it does not change.",
            "would_change": False,
            "reason": "the plan names the file as context only.",
        },
        {
            "id": "scope-2",
            "type": "scope",
            "text": "Section five duplicates section two.",
            "would_change": False,
            "reason": "the duplication is deliberate and stated.",
        },
        {
            "id": "scope-3",
            "type": "scope",
            "text": "The duplication changes what the node builds.",
            "would_change": True,
            "reason": "the node builds both copies.",
        },
    ]


def _committed_record(run_id: str, findings: list[dict]) -> Path:
    """Store a review in the project's committed tree and commit it."""
    root = review_store.committed_review_root("sample")
    assert root is not None
    path = plan_review.plan_review_path(
        "sample", "fixture", 3, committed_root=root, review_run_id=run_id
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "project": "sample",
                "plan_slug": "fixture",
                "plan_version": 3,
                "review_run_id": run_id,
                "rubric": "content",
                "findings": findings,
                "responses": {},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    checkout = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for args in (
        ["add", str(path.relative_to(checkout))],
        ["commit", "-q", "-m", "test: seed a stored review", "-m", "Seed it."],
    ):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)
    return path


def _commits(checkout) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(checkout), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in listed.splitlines() if line]


def test_a_quiet_plan_with_a_recent_run_is_composed_under_that_runs_session(
    project, monkeypatch
):
    _home, repo, path = project
    launched = _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)

    report = _sweep_pass()

    composed = [row for row in report["composed"] if row["plan"] == "fixture"]
    assert composed, report
    assert composed[0]["dispatched"] is True
    assert composed[0]["review_run_id"] == "composed-review"
    # The review runs under the plan's own authoring session, not under the
    # session that happened to run the sweep.
    assert composed[0]["session"] == AUTHOR_SESSION
    assert launched == [AUTHOR_SESSION], launched
    assert "sweeper" not in launched
    # Nor does the launcher receive a lane: a review naming none takes the
    # review lanes, which keeps it off a metered one and holds it when the
    # local lane is saturated.
    assert SUBJECTS and SUBJECTS[-1].get("local") is None, SUBJECTS
    assert SUBJECTS[-1].get("rubric") == "content", SUBJECTS
    # A review was really composed, not merely reported: its brief, its plan
    # snapshot and its sidecar are on disk for the queued run to read.
    directories = _run_directories()
    assert len(directories) == 1
    assert (directories[0] / "brief.md").is_file()
    assert (directories[0] / "plan.html").read_bytes() == path.read_bytes()
    sidecar = (directories[0] / "plan-review.json").read_text()
    assert '"rubric": "content"' in sidecar
    assert repo is not None


def test_a_plan_edited_two_minutes_ago_is_not_composed(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, FRESH_EDIT_SECONDS)
    _run_naming()

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped, report
    assert "minutes ago" in skipped[0]["reason"]
    assert _run_directories() == []


def test_a_plan_edited_two_days_ago_is_not_composed_though_it_owes_a_review(
    project, monkeypatch
):
    _home, _repo, path = project
    _launch(monkeypatch)
    # The plan owes a review — it is quiet, nothing covers it and nothing is in
    # flight — and its author ran it an hour ago. Only its edit's age refuses it.
    _edit(path, STALE_EDIT_SECONDS)
    _run_naming(session=AUTHOR_SESSION)

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped, report
    assert "authoring burst" in skipped[0]["reason"], skipped[0]
    assert _run_directories() == []


def test_a_plan_no_run_has_named_in_the_last_day_is_not_composed(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    # A run names the plan, but it is two days old: nobody is working on the
    # plan now, so a review composed for it would reach no reader.
    _run_naming(seconds_ago=STALE_RUN_SECONDS)

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped and "no crew run has named the plan" in skipped[0]["reason"]
    assert _run_directories() == []


def test_a_run_on_the_plan_is_read_from_the_ledger_as_well(project):
    _home, _repo, _path = project
    moment = (datetime.now(UTC) - timedelta(seconds=AUTHOR_RUN_SECONDS)).isoformat()
    ledger.run_path("sample", "r-fixture-completed").parent.mkdir(
        parents=True, exist_ok=True
    )
    ledger.write_run_file(
        "sample",
        {
            "run_id": "r-fixture-completed",
            "project": "sample",
            "plan": "fixture",
            "session": AUTHOR_SESSION,
            "subject": "plan",
            "status": "complete",
            "dispatched_at": moment,
            "completed_at": moment,
        },
    )

    author = plan_review.plan_authoring_run("sample", "fixture")

    assert author is not None, "a committed ledger row must name the plan's author"
    assert author["session"] == AUTHOR_SESSION
    assert author["run_id"] == "r-fixture-completed"


def test_a_plan_with_a_review_in_flight_is_not_composed(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming()
    monkeypatch.setattr(
        recovery_review_dispatch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": AUTHOR_SESSION,
                "node": {"id": "plan-review-of-fixture"},
                "phase": "working",
            }
        ],
    )

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped and "standing-review" in skipped[0]["reason"]
    assert _run_directories() == []


def test_would_not_change_findings_of_a_composed_review_are_noted(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    # A fresh edit keeps composition out of the way: the notes are answered by
    # a pass that composes nothing, so an answer cannot be mistaken for one.
    _edit(path, FRESH_EDIT_SECONDS)
    _stored_review(review_run_id="composed-review", findings=_two_findings())
    _composed_by_the_reflex("composed-review")

    report = _pass()

    noted = sorted(row["finding"] for row in report["notes"])
    assert noted == ["scope-1", "scope-2"], report
    record = plan_review.list_plan_reviews("sample")[0]
    assert record["responses"]["scope-1"]["action"] == plan_review.NOTED_ACTION
    assert (
        record["responses"]["scope-1"]["reason"]
        == "the plan names the file as context only."
    )
    assert record["responses"]["scope-1"]["by"] == plan_review.PLAN_REVIEW_REFLEX
    # The finding that would change the plan waits for its author, and every
    # finding on the record still reads as unanswered until it is answered.
    assert plan_review.unanswered_findings(record) == ["scope-3"]


def test_a_review_the_pass_did_not_compose_gains_no_answer_and_no_commit(
    project, monkeypatch
):
    _home, repo, path = project
    _launch(monkeypatch)
    _edit(path, FRESH_EDIT_SECONDS)
    # This review exists, carries a would-not-change finding, and was composed
    # by a coordinator rather than by the reflex — the reflex has recorded no
    # such dispatch, so the record is not its to answer.
    committed = _committed_record("someone-elses-review", _two_findings())
    before = _commits(repo)
    _composed_by_the_reflex("composed-review")

    report = _pass()

    assert report["notes"] == [], report
    record = json.loads(committed.read_text(encoding="utf-8"))
    assert record["responses"] == {}
    assert _commits(repo) == before, "an untouched review must not gain a commit"


def test_a_pass_that_notes_two_findings_makes_one_commit(project, monkeypatch):
    _home, repo, path = project
    _launch(monkeypatch)
    _edit(path, FRESH_EDIT_SECONDS)
    _committed_record("composed-review", _two_findings())
    _composed_by_the_reflex("composed-review")
    before = _commits(repo)

    report = _pass()

    assert sorted(row["finding"] for row in report["notes"]) == ["scope-1", "scope-2"]
    gained = _commits(repo)[: len(_commits(repo)) - len(before)]
    assert len(gained) == 1, gained
    record = plan_review.list_plan_reviews("sample")[0]
    assert record["responses"]["scope-1"]["action"] == plan_review.NOTED_ACTION
    assert record["responses"]["scope-2"]["action"] == plan_review.NOTED_ACTION


def test_a_plan_whose_review_is_stored_is_not_composed_again(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming()
    _stored_review(
        plan_fingerprint=plan_review.plan_fingerprint(path),
        reviewed_blob_sha="a" * 40,
        timestamp=datetime.now(UTC).isoformat(),
    )

    report = _pass()

    assert [row for row in report["composed"] if row["plan"] == "fixture"] == []
    skipped = [row for row in report["skipped"] if row.get("plan") == "fixture"]
    assert skipped and "already covers" in skipped[0]["reason"]


def test_the_quiet_period_is_measured_from_the_plan_file(project):
    _home, _repo, path = project
    _edit(path, QUIET_EDIT_SECONDS)
    assert plan_review.plan_review_quiet_remaining(path) == 0
    _edit(path, FRESH_EDIT_SECONDS)
    remaining = plan_review.plan_review_quiet_remaining(path)
    assert 0 < remaining <= plan_review.PLAN_REVIEW_QUIET_SECONDS - FRESH_EDIT_SECONDS


def test_the_burst_window_is_measured_from_the_same_edit(project):
    _home, _repo, path = project
    _edit(path, QUIET_EDIT_SECONDS)
    age = plan_review.plan_review_edit_age(path)
    assert age is not None
    assert QUIET_EDIT_SECONDS <= age < QUIET_EDIT_SECONDS + 60
    assert age < plan_review.PLAN_REVIEW_BURST_SECONDS
    _edit(path, STALE_EDIT_SECONDS)
    stale = plan_review.plan_review_edit_age(path)
    assert stale is not None and stale > plan_review.PLAN_REVIEW_BURST_SECONDS


def test_a_pass_reads_only_the_plans_edited_since_the_last_one(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _run_naming()
    template = (
        "<!doctype html><html><head>"
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="{slug}">'
        '<meta name="plan-status" content="active">'
        "</head><body><p>Bulk.</p></body></html>"
    )
    for index in range(59):
        slug = f"bulk-{index:02d}"
        sibling = path.parent / f"{slug}.html"
        sibling.write_text(template.format(slug=slug), encoding="utf-8")
        _edit(sibling, QUIET_EDIT_SECONDS)
    _edit(path, QUIET_EDIT_SECONDS)

    # The pass has already weighed all sixty, as a pass before the count began
    # would have left them.
    plans, failed = plan_review._project_plan_paths("sample")
    assert failed == []
    assert len(plans) == 60
    plan_review._record_weighed_edits(
        "sample", {slug: target.stat().st_mtime for slug, target in plans}
    )
    # One of them is written.
    _edit(path, QUIET_EDIT_SECONDS - 60)

    report = _pass()

    assert report["read"] == ["fixture"], report


def test_a_plan_the_catalogue_refuses_is_reported_and_the_rest_compose(
    project, monkeypatch
):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming()
    # A document whose location says research and whose declared type says plan
    # makes the catalogue refuse the whole tree.
    conflicting = path.parent.parent / "research" / "contour-mesh-capacity.html"
    conflicting.parent.mkdir(parents=True, exist_ok=True)
    conflicting.write_text(path.read_text(), encoding="utf-8")

    report = _pass()

    assert [row["plan"] for row in report["composed"]] == ["fixture"], report
    assert any(
        "contour-mesh-capacity" in str(row.get("plan", "")) for row in report["failed"]
    ), report


def test_a_refused_dispatch_is_retried_on_the_next_pass(project, monkeypatch):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming()
    # The refusal the dispatch hands back when a lane holds the review and the
    # request queues nothing.
    real_dispatch = recovery.dispatch_review_for_run
    monkeypatch.setattr(
        recovery,
        "dispatch_review_for_run",
        lambda *args, **kwargs: {
            "dispatched": False,
            "reason": "the metered lane is held",
        },
    )

    first = _pass()

    assert first["composed"], first["failed"]
    assert first["composed"][0]["dispatched"] is False
    # A hold that launched nothing leaves the plan owing its review, so the
    # next pass composes it again rather than waiting for a new edit.
    monkeypatch.setattr(recovery, "dispatch_review_for_run", real_dispatch)
    second = _pass()

    assert [row["plan"] for row in second["composed"]] == ["fixture"], second
    assert second["composed"][0]["dispatched"] is True


def test_a_plan_whose_reason_never_settles_does_not_starve_the_rest(
    project, monkeypatch
):
    _home, _repo, path = project
    _launch(monkeypatch)
    # A plan that sorts before the quiet one and whose review is in flight: the
    # reason is true on every pass, so a pass that always started at the first
    # plan would spend its single slot on it and never reach the quiet plan.
    holder = path.parent / "anchor.html"
    holder.write_text(
        "<!doctype html><html><head>"
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="anchor">'
        '<meta name="plan-status" content="active">'
        "</head><body><p>Anchor.</p></body></html>",
        encoding="utf-8",
    )
    _edit(holder, QUIET_EDIT_SECONDS)
    _edit(path, QUIET_EDIT_SECONDS)
    _run_naming("anchor")
    _run_naming("fixture")
    monkeypatch.setattr(
        recovery_review_dispatch,
        "list_live",
        lambda **kw: [
            {
                "run_id": "standing-review",
                "project": "sample",
                "session": AUTHOR_SESSION,
                "node": {"id": "plan-review-of-anchor"},
                "phase": "working",
            }
        ],
    )

    first = _pass()
    second = _pass()

    assert [row["plan"] for row in first["composed"]] == [], first
    assert [row["plan"] for row in second["composed"]] == ["fixture"], second


@pytest.mark.parametrize("seconds_ago", [QUIET_EDIT_SECONDS, FRESH_EDIT_SECONDS])
def test_a_dry_run_composes_nothing_and_answers_nothing(
    project, monkeypatch, seconds_ago
):
    _home, _repo, path = project
    _launch(monkeypatch)
    _edit(path, seconds_ago)
    _run_naming()
    stored = _stored_review(review_run_id="composed-review", findings=_two_findings())
    _composed_by_the_reflex("composed-review")

    report = plan_review.compose_owed_plan_reviews("sample", dry_run=True)

    assert report["notes"] == []
    assert json.loads(stored.read_text(encoding="utf-8")).get("responses", {}) == {}
    assert _run_directories() == []
