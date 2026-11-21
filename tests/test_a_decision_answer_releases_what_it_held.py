"""An answer reaches its owner and releases what it held.

An answer is accepted only through the session presenting a decision. It stores
the chosen option, the note and the time, the owning session reads it as a
duty, the project's monitor stream gains one row for it, and the requests held
behind the decision leave their hold at the next admission pass — including
when the owner has no live session and when the decision was written in a
plan's decisions block and answered by locking it there.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon._plan_html import read_state_file, write_state
from reckon.crew import decision_answers, run_paths, runs
from reckon.crew.lead_decisions import record_decision
from reckon.crew.obligations import obligations
from reckon.mcp_views import serve_crew_view

PROJECT = "sample"
OWNER = "owner-session"
PRESENTER = "presenting-session"
PLAN = "transport"
SECTION = "payload"


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: Path) -> tuple[Path, Path]:
    """A temporary crew home with one mounted project, nothing outside it."""
    home = tmp_path / "config"
    home.mkdir()
    docs = tmp_path / PROJECT / "docs"
    (docs / "plans").mkdir(parents=True)
    registry = home / "mounts.json"
    registry.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    repo = docs.parent
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "seed"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(registry))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(home / "state"))
    return home, docs


def _question(*, held: tuple[str, ...] = (), session: str = OWNER) -> dict:
    """Record one open question for the lead, holding ``held`` behind it."""
    return record_decision(
        {
            "question": "Which transport carries the work?",
            "context": "The current transport is saturated. A second route exists.",
            "options": [
                {"value": "wait", "label": "Wait for capacity"},
                {"value": "route", "label": "Use the second route"},
            ],
            "recommended": "route",
            "project": PROJECT,
            "session": session,
            "plan": PLAN,
            "section": SECTION,
            "held_requests": list(held),
        }
    )


def _held_pointer(home: Path, run_id: str, decision_id: str) -> Path:
    """Write one request held behind ``decision_id``, as a coordinator holds it."""
    path = home / "crew" / "live" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "phase": "held",
                "session": OWNER,
                "node": {"plan": PLAN, "section": SECTION},
                "waits": [],
                "waits_at": "2026-11-01T00:00:00+00:00",
                "queued_at": "2026-11-01T00:00:00+00:00",
                "queue_hold": {
                    "prior_phase": "queued",
                    "started_at": "2026-11-02T00:00:00+00:00",
                    "reason": "waiting for the lead",
                    "decision_id": decision_id,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _answer(decision_id: str, *, choice: str = "route", note: str = "") -> dict:
    """Claim the decision for one presenting session, then answer it."""
    decision_answers.claim_presentation(decision_id, session=PRESENTER)
    return decision_answers.answer_decision(
        decision_id, session=PRESENTER, choice=choice, note=note
    )


def _monitor_rows(project: str) -> list[dict]:
    path = run_paths.watch_stream_path(project)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_an_answer_stores_the_choice_and_helped_requests_leave_hold(crew_home):
    home, _ = crew_home
    decision = _question(held=("run-a", "run-b"))
    _held_pointer(home, "run-a", decision["id"])
    _held_pointer(home, "run-b", decision["id"])

    _answer(decision["id"], choice="route", note="take the second route")

    # The store is the assertion: the returned record is what the caller passed
    # in plus one time, so comparing it with the input proves nothing about
    # whether the answer was written.
    stored = json.loads(
        (home / "crew" / "decisions" / f"{decision['id']}.json").read_text()
    )
    assert stored["choice"] == "route"
    assert stored["answer_note"] == "take the second route"
    assert stored["answered_at"]

    rows = _monitor_rows(PROJECT)
    assert len(rows) == 1
    assert rows[0]["kind"] == "decision-answered"
    assert rows[0]["decision_id"] == decision["id"]
    assert rows[0]["choice"] == "route"

    owner = obligations(PROJECT, OWNER)["obligations"]
    duties = [row for row in owner if row["kind"] == "decision-answered"]
    assert len(duties) == 1
    assert duties[0]["decision_id"] == decision["id"]
    assert duties[0]["choice"] == "route"
    assert duties[0]["run_ids"] == ["run-a", "run-b"]
    other = obligations(PROJECT, PRESENTER)["obligations"]
    assert [row for row in other if row["kind"] == "decision-answered"] == []

    assert sorted(decision_answers.release_answered_holds(runs.list_live())) == [
        "run-a",
        "run-b",
    ]
    for run_id in ("run-a", "run-b"):
        assert runs.read_pointer(run_id)["phase"] == "queued"
    # A second pass finds nothing held: the release is idempotent.
    assert decision_answers.release_answered_holds(runs.list_live()) == []


def test_an_answer_outside_the_presenting_session_is_refused(crew_home):
    home, _ = crew_home
    decision = _question(held=("run-a",))
    _held_pointer(home, "run-a", decision["id"])

    with pytest.raises(ValueError, match="not being presented"):
        decision_answers.answer_decision(
            decision["id"], session=PRESENTER, choice="route"
        )

    decision_answers.claim_presentation(decision["id"], session=PRESENTER)
    claimed = (home / "crew" / "decisions" / f"{decision['id']}.json").read_text()
    for session in (OWNER, "worker-session"):
        with pytest.raises(ValueError, match="presented by 'presenting-session'"):
            decision_answers.answer_decision(
                decision["id"], session=session, choice="route"
            )
    assert (
        home / "crew" / "decisions" / f"{decision['id']}.json"
    ).read_text() == claimed
    assert _monitor_rows(PROJECT) == []
    assert runs.read_pointer("run-a")["phase"] == "held"


def test_an_owner_with_no_live_session_is_answered_and_releases(crew_home):
    home, _ = crew_home
    decision = _question(held=("orphan-run",), session="ended-session")
    _held_pointer(home, "orphan-run", decision["id"])

    saved = _answer(decision["id"], choice="wait")

    assert saved["choice"] == "wait"
    assert decision_answers.release_answered_holds(runs.list_live()) == ["orphan-run"]
    assert runs.read_pointer("orphan-run")["phase"] == "queued"
    duties = [
        row
        for row in obligations(PROJECT, "ended-session")["obligations"]
        if row["kind"] == "decision-answered"
    ]
    assert len(duties) == 1
    assert duties[0]["decision_id"] == decision["id"]


def _plan_with_decision(docs: Path) -> Path:
    """A plan whose decisions block names one unanswered decision."""
    path = docs / "plans" / f"{PLAN}.html"
    path.write_text(
        write_state(
            "<html><head>"
            '<meta name="docs-project" content="sample">'
            "<title>Transport</title></head><body>"
            '<h2 id="payload">Payload</h2></body></html>',
            {
                "slug": PLAN,
                "version": 0,
                "status": "active",
                "modified": "2026-10-01",
                "decisions": {
                    "format": {
                        "title": "Which payload format?",
                        "context": "Readers need one representation.",
                        "choices": ["compact", "expanded"],
                        "option_labels": {"compact": "Compact", "expanded": "Expanded"},
                        "recommended": "expanded",
                        "sections": [SECTION],
                    }
                },
            },
        ),
        encoding="utf-8",
    )
    return path


def test_a_plan_decision_is_locked_in_its_plan_and_releases(crew_home):
    home, docs = crew_home
    path = _plan_with_decision(docs)
    decision_id = f"plan:{PROJECT}:{PLAN}:format"
    _held_pointer(home, "plan-request", decision_id)

    rows = serve_crew_view("decisions")["decisions"]
    assert [row["id"] for row in rows] == [decision_id]
    assert rows[0]["held_requests"] == ["plan-request"]

    _answer(decision_id, choice="expanded")

    assert read_state_file(path)["decisions"]["format"]["choice"] == "expanded"
    after = serve_crew_view("decisions")
    assert after.get("decisions") == [], after
    assert decision_answers.release_answered_holds(runs.list_live()) == ["plan-request"]
    assert runs.read_pointer("plan-request")["phase"] == "queued"


def test_the_released_phase_reaches_the_pass_that_released_it(crew_home):
    home, _ = crew_home
    decision = _question(held=("run-a",))
    _held_pointer(home, "run-a", decision["id"])
    _answer(decision["id"], choice="route")

    live = runs.list_live()
    assert [pointer["phase"] for pointer in live] == ["held"]
    released = decision_answers.release_answered_holds(live)

    assert released == ["run-a"]
    # The pass judges the request it just released in the same walk, so the
    # in-memory pointer must carry the released phase, not the held one.
    assert live[0]["phase"] == "queued"
    assert "queue_hold" not in live[0]


def test_the_answer_store_refuses_an_option_the_question_never_offered(crew_home):
    home, _ = crew_home
    decision = _question(held=("run-a",))
    _held_pointer(home, "run-a", decision["id"])
    path = home / "crew" / "decisions" / f"{decision['id']}.json"
    before = path.read_text(encoding="utf-8")

    decision_answers.claim_presentation(decision["id"], session=PRESENTER)
    with pytest.raises(ValueError, match="is not one of"):
        decision_answers.answer_decision(
            decision["id"], session=PRESENTER, choice="teleport"
        )

    assert path.read_text(encoding="utf-8") == before
    assert _monitor_rows(PROJECT) == []
    assert runs.read_pointer("run-a")["phase"] == "held"
