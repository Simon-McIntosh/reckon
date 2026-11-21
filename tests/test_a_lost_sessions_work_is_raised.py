"""A lost session's implement work is raised as one decision for the lead.

A session nothing has heard for the flight threshold owns work nobody can
answer for. Its reviews are retired by the review rule; its implement work is
raised here as exactly one open decision listing every record it still owns,
with the project, plan, section, phase and age, and the work each one holds
back. The decision is written once and updated in place; a pass that finds the
same list writes nothing.

The lead answers it per record: adopt moves a record's ownership to a live
session, retire removes a request with a discard record or records a run's
disposition without promoting it. Until the decision is answered, the session's
implement requests launch nothing and its landing groups are skipped, one
reason recorded rather than a refusal stamped on every pass.

The negative control empties the decision store between passes: with nothing to
find, the raise writes on every pass, so the test asserting a single write fails.
"""

from __future__ import annotations

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import lost_session_work as raised
from reckon.crew import runs
from tests.test_a_queued_run_is_admitted import (  # noqa: F401 - fixture
    PROJECT,
    _config_for,
    _launcher,
    _queue,
    admit_repo,
)

HOUR = 3600.0
LOST_SESSION = "lost-coordinator"
LIVE_SESSION = "live-coordinator"


def _admit(config_for, launcher, *, run_id: str = ""):
    from reckon.crew import queue_admission

    return queue_admission.admit_once(
        config_for=config_for, launcher=launcher, run_id=run_id
    )


def _age_pointer(
    run_id: str,
    *,
    session: str,
    age_hours: float,
    phase: str | None = None,
    role: str = "implement",
) -> dict:
    """Rewrite a pointer's own clock and phase, so its session ages by it."""
    stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()

    def rewrite(current: dict) -> dict:
        current["session"] = session
        current["created_at"] = stamp
        current["queued_at"] = stamp
        current["role"] = role
        if phase is not None:
            current["phase"] = phase
        return current

    runs._mutate_pointer(run_id, rewrite)
    return runs.read_pointer(run_id)


def _pointers(repository: Path) -> None:
    """One queued request, one blocked run and one finished run, all lost."""

    runs.queue_dispatch(
        _queue(repository, node_id="queued-node", run_id="r-lost-queued")
    )
    runs.queue_dispatch(
        _queue(repository, node_id="blocked-node", run_id="r-lost-blocked")
    )
    runs.queue_dispatch(
        _queue(repository, node_id="finished-node", run_id="r-lost-finished")
    )
    _age_pointer("r-lost-queued", session=LOST_SESSION, age_hours=73, phase="queued")
    _age_pointer("r-lost-blocked", session=LOST_SESSION, age_hours=73, phase="blocked")
    _age_pointer(
        "r-lost-finished", session=LOST_SESSION, age_hours=73, phase="complete"
    )


def _decisions() -> list[dict]:
    return raised.open_decisions()


def test_one_decision_names_every_record_and_ten_passes_write_once(
    admit_repo: Path,
) -> None:
    _config, resolve = _config_for()
    _pointers(admit_repo)
    recorded: list = []
    writes: list[str] = []

    first = _admit(resolve, _launcher(recorded))
    writes.extend(first["lost_session_decision"]["written"])

    decisions = _decisions()
    assert len(decisions) == 1, decisions
    record = decisions[0]
    named = {row["run_id"] for row in record["records"]}
    assert named == {"r-lost-queued", "r-lost-blocked", "r-lost-finished"}
    for row in record["records"]:
        assert row["project"] == PROJECT
        assert row["plan"] == "plan-a"
        assert row["section"] == "dispatch"
        assert row["phase"]
        assert row["age_hours"] and row["age_hours"] >= 72
        assert row["holds_back"]

    # The decision is readable through the crew view, not only on disk.
    from reckon.crew.lead_decisions import read_decisions

    view = read_decisions(PROJECT)
    assert [row["id"] for row in view["decisions"]] == [record["id"]], view["decisions"]

    for _ in range(10):
        later = _admit(resolve, _launcher([]))
        writes.extend(later["lost_session_decision"]["written"])
        assert len(_decisions()) == 1, (
            "a re-raise updates in place, never adds a second"
        )

    assert len(writes) == 1, f"the decision is written once, not per pass: {writes}"
    assert recorded == [], "a lost session's implement requests launch nothing"


def test_emptying_the_store_makes_the_raise_write_more_than_once(
    admit_repo: Path,
) -> None:
    """The negative control: with nothing to find, every pass writes.

    The single-write assertion above fails here by counting more than one write,
    which is what shows the assertion measures reuse of the record rather than a
    pass that never wrote at all.
    """
    _config, resolve = _config_for()
    _pointers(admit_repo)
    writes: list[str] = []
    for _ in range(10):
        # Empty the store between passes, forcing a fresh write each time.
        for path in (raised._decisions_dir()).glob("*.json"):
            path.unlink()
        result = _admit(resolve, _launcher([]))
        writes.extend(result["lost_session_decision"]["written"])
    assert len(writes) > 1, f"the control must count more than one write: {writes}"


def test_an_adopt_answer_moves_ownership_to_the_adopter(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import lost_sessions

    monkeypatch.setattr(
        lost_sessions, "live_process_sessions", lambda sessions: {LIVE_SESSION}
    )
    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    record = _decisions()[0]

    answered = raised.answer_decision(
        record["id"],
        {"r-lost-queued": {"action": "adopt", "session": LIVE_SESSION}},
        answered_by="lead",
    )

    assert answered["adopted"] == ["r-lost-queued"]
    assert runs.read_pointer("r-lost-queued")["session"] == LIVE_SESSION
    assert [p["run_id"] for p in runs.list_live() if p["session"] == LIVE_SESSION] == [
        "r-lost-queued"
    ]
    assert [p["run_id"] for p in runs.list_live() if p["session"] == LOST_SESSION] == [
        "r-lost-blocked",
        "r-lost-finished",
    ]
    assert record["id"] not in {d["id"] for d in _decisions()}, (
        "an answered decision leaves the open view"
    )


def test_the_raised_decision_is_read_and_validated_by_the_store(
    admit_repo: Path,
) -> None:
    """The decision is the decisions store's own record, not one beside it."""
    from reckon.crew.lead_decisions import read_decisions, validate_decision

    _config, resolve = _config_for()
    _pointers(admit_repo)
    result = _admit(resolve, _launcher([]))

    written = result["lost_session_decision"]["written"]
    assert len(written) == 1, written
    view = read_decisions(PROJECT)
    rows = [row for row in view["decisions"] if row["id"] == written[0]]
    assert rows, view
    validated = validate_decision(rows[0])
    assert validated["project"] == PROJECT
    assert validated["session"] == LOST_SESSION
    assert {row["run_id"] for row in rows[0]["records"]} == {
        "r-lost-queued",
        "r-lost-blocked",
        "r-lost-finished",
    }


def test_an_adopt_naming_a_session_with_no_live_process_is_refused(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from reckon.crew import lost_sessions

    monkeypatch.setattr(lost_sessions, "live_process_sessions", lambda sessions: set())
    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    record = _decisions()[0]
    before = runs.read_pointer("r-lost-queued")

    with pytest.raises(ValueError, match="no live process"):
        raised.answer_decision(
            record["id"],
            {"r-lost-queued": {"action": "adopt", "session": "ghost-session"}},
        )

    assert runs.read_pointer("r-lost-queued") == before, (
        "a refused adopt leaves the pointer untouched"
    )
    assert record["id"] in {d["id"] for d in _decisions()}, (
        "a refused answer leaves the decision open"
    )


def test_an_answer_naming_a_vanished_run_returns_the_decision_error(
    admit_repo: Path,
) -> None:
    from click.testing import CliRunner

    from reckon.cli import main

    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    record = _decisions()[0]

    result = CliRunner().invoke(
        main,
        ["crew", "decision", "answer", "--id", record["id"], "--answers", "-"],
        input=json.dumps({"r-vanished": {"action": "retire"}}),
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"] == "decision_error"
    assert "r-vanished" in payload["detail"]
    assert record["id"] in {d["id"] for d in _decisions()}, (
        "a refused answer leaves the decision open"
    )


def test_a_retire_answer_leaves_a_discard_record_and_no_pointer(
    admit_repo: Path,
) -> None:
    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    record = _decisions()[0]

    answered = raised.answer_decision(
        record["id"],
        {
            "r-lost-queued": {"action": "retire"},
            "r-lost-blocked": {"action": "retire"},
            "r-lost-finished": {"action": "retire"},
        },
        answered_by="lead",
    )

    assert set(answered["retired"]) == {
        "r-lost-queued",
        "r-lost-blocked",
        "r-lost-finished",
    }
    from reckon.crew import promotion

    for run_id in answered["retired"]:
        assert not runs.pointer_path(run_id).exists(), run_id
        marker = next(
            path
            for path in (
                promotion.discard_record_path(run_id),
                promotion.discard_spool_path(run_id),
            )
            if path.is_file()
        )
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert "answer" in data["reason"], data["reason"]
        assert data["lost_session"] == LOST_SESSION


def test_an_already_promoted_pointer_is_retired_without_a_decision(
    admit_repo: Path,
) -> None:
    from reckon.crew import promotion

    _config, resolve = _config_for()
    runs.queue_dispatch(
        _queue(admit_repo, node_id="promoted-node", run_id="r-lost-promoted")
    )

    def promote(current: dict) -> dict:
        current["session"] = LOST_SESSION
        current["phase"] = "promoted"
        current["promoted_at"] = datetime.now(UTC).isoformat()
        current["created_at"] = (datetime.now(UTC) - timedelta(hours=73)).isoformat()
        return current

    runs._mutate_pointer("r-lost-promoted", promote)

    result = _admit(resolve, _launcher([]))

    assert result["lost_session_decision"]["retired"] == ["r-lost-promoted"]
    assert not runs.pointer_path("r-lost-promoted").exists()
    assert _decisions() == [], "an already-promoted pointer needs no decision"
    marker = promotion.discard_record_path("r-lost-promoted")
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["retired_reason"] == "already-promoted"


def test_a_landing_of_a_lost_sessions_member_is_skipped_not_refused(
    admit_repo: Path,
) -> None:
    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    pointer = runs.read_pointer("r-lost-blocked")
    before = dict(pointer)

    first = raised.skip_lost_session_landings(pointer)
    second = raised.skip_lost_session_landings(pointer)

    assert first and first == second, "one reason, the same on every pass"
    assert "lost" in first
    assert runs.read_pointer("r-lost-blocked") == before, (
        "a skip records no refusal on the pointer"
    )


def test_the_lander_skips_a_lost_sessions_delivery_and_records_the_reason(
    admit_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is reached through the lander, not only by calling it direct."""
    from contextlib import nullcontext

    from reckon.crew import queue_admission, queue_landing

    _config, resolve = _config_for()
    _pointers(admit_repo)
    _admit(resolve, _launcher([]))
    monkeypatch.setattr(queue_admission, "plan_read_scopes", nullcontext)

    result = queue_landing.land_once()

    skipped = {
        row["run_id"]: row
        for row in result.get("skipped") or ()
        if row.get("status") == "lost-session"
    }
    assert "r-lost-blocked" in skipped, result
    assert "lost" in skipped["r-lost-blocked"]["reason"]
    assert result["landed"] == []


# --------------------------------------------------------------------------
# A stranded request is retired by the wait rule, not raised by this one.
# --------------------------------------------------------------------------


def _chain_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests import test_a_queued_request_waits_on_its_dependencies as chain

    return chain.chain_repo.__wrapped__(tmp_path, monkeypatch)


def test_a_stranded_request_is_retired_and_absent_from_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stranded request is retired by the wait rule, not listed here.

    The lost session owns both a request whose upstream can never land and an
    implement request whose work is still live. The first is retired by the wait
    rule with its reason; the decision names only the second.
    """
    from reckon.crew import lost_sessions, queue_admission
    from tests import test_a_queued_request_waits_on_its_dependencies as chain
    from tests import test_a_wait_that_cannot_resolve_is_removed as waits

    repository = _chain_repo(tmp_path, monkeypatch)
    chain._declare_wait_refs(repository, section="s2", refs=[f"{chain.PLAN}#s1"])
    waits._queue(
        repository, waits._node("waiting", "s2"), run_ids={"waiting": "r-stranded"}
    )
    waits._queue(
        repository, waits._node("raising", "s3"), run_ids={"raising": "r-raising"}
    )
    waits._drop_declaration(repository, "s1")
    stale = (datetime.now(UTC) - timedelta(hours=73)).isoformat()
    for run_id in ("r-stranded", "r-raising"):
        pointer = runs.read_pointer(run_id)
        pointer["session"] = LOST_SESSION
        pointer["created_at"] = stale
        pointer["waits_at"] = stale
        runs._write_json(runs.pointer_path(run_id), pointer)

    live = runs.list_live(project=chain.PROJECT)
    outcome = queue_admission.advance_waiting_requests(live)
    assert [row["run_id"] for row in outcome["retired"]] == ["r-stranded"]
    reason = outcome["retired"][0]["reason"]
    assert "carries no section" in reason, reason

    remaining = runs.list_live(project=chain.PROJECT)
    answers = lost_sessions.LostSessionReads(pointers=remaining).answers(
        [(str(p.get("project") or ""), str(p.get("session") or "")) for p in remaining]
    )
    raised.raise_lost_session_work(remaining, answers=answers)

    decisions = _decisions()
    assert len(decisions) == 1, decisions
    named = {row["run_id"] for row in decisions[0]["records"]}
    assert named == {"r-raising"}, "the stranded request is absent from the decision"
