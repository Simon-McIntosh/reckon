"""A second complete finishes what an interrupted promotion left behind.

A promotion writes the plan comment, then the ledger row, then retires the live
pointer and releases the worktree. A landing that stops after the row leaves a
run that is promoted by the record and unreconciled by every reader that looks
at the live directory: no recovery verb clears it, because the row already
exists. A second complete must therefore finish the remaining cleanup instead
of refusing, and it must not record the run again.

The narrative is the part a second call cannot repeat byte-identically. The
coordinator who retries describes the outcome in their own words, and the plan
comment written by the first promotion already holds another. The committed
ledger row is the record of that promotion, so the difference is reported in
the receipt while the existing comment is kept.

Two cases run in a temporary config home: a run whose row is written and whose
pointer survived is retired with no second row, and the same run retried with a
different narrative finishes the cleanup and names the difference. The third is
the ordinary promotion of a run with no row, which still writes one.
"""

# ruff: noqa: F811 - the fixture is imported and also named as each test's parameter.

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html, crew, ledger
from reckon.crew import promotion, promotion_scope
from reckon.crew.node import CrewError
from reckon.crew.runs import pointer_path
from tests.test_a_run_can_always_be_reconciled import (  # noqa: F401 - fixture
    PLAN,
    PROJECT,
    _pointer,
    repository,
)


def _promote(repository: Path, run_id: str, *, outcome: str) -> dict:
    return crew.complete(run_id, gate="not-run", outcome=outcome, root=repository)


def _landing_comment_bodies(repository: Path) -> list[str]:
    state = _plan_html.read_state_file(repository / "docs" / "plans" / f"{PLAN}.html")
    return [
        item.get("body", "")
        for items in (state.get("comments") or {}).values()
        for item in items
    ]


# ── Case 1: a written row and a surviving pointer ───────────────────────────


def test_a_second_complete_retires_the_pointer_and_writes_no_second_row(
    repository: Path,
) -> None:
    """The cleanup a promotion left undone is finished by the next call."""
    run_id = "r-second-complete-same-narrative"
    outcome = "reviewed and landed before the pointer was retired"
    _pointer(repository, run_id)
    first = _promote(repository, run_id, outcome=outcome)
    assert first["already_promoted"] is False
    assert len(ledger.runs(PROJECT, root=repository)) == 1

    # The state the interrupted promotion leaves: row committed, pointer still
    # on disk, so the run reads as live to every reader of the live directory.
    _pointer(repository, run_id)
    second = _promote(repository, run_id, outcome=outcome)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1
    assert second["plan_comment"]["finished_cleanup"] is True
    assert "cleanup" in second["plan_comment"]["note"]


# ── Case 2: the retry's narrative differs from the recorded one ─────────────


def test_a_differing_narrative_finishes_the_cleanup_and_is_named(
    repository: Path,
) -> None:
    """The row is the record, so a new narrative is reported, not refused."""
    run_id = "r-second-complete-other-narrative"
    recorded = "reviewed and landed before the pointer was retired"
    retried = "relearning the landing in the coordinator's own words"
    _pointer(repository, run_id)
    _promote(repository, run_id, outcome=recorded)
    _pointer(repository, run_id)

    second = _promote(repository, run_id, outcome=retried)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    assert len(ledger.runs(PROJECT, root=repository)) == 1
    comment = second["plan_comment"]
    assert comment["finished_cleanup"] is True
    assert comment["reason"] == "different_narrative"
    assert comment["kept_narrative"] == recorded
    assert recorded in comment["note"]
    # The comment keeps the narrative the first promotion wrote.
    assert any(recorded in body for body in _landing_comment_bodies(repository))
    assert not any(retried in body for body in _landing_comment_bodies(repository))


# ── Case 3: no row yet, the ordinary promotion ──────────────────────────────


def test_a_run_with_no_ledger_row_is_promoted(
    repository: Path,
) -> None:
    """A run that never landed records its row and retires its pointer."""
    run_id = "r-first-promotion"
    _pointer(repository, run_id)

    receipt = _promote(repository, run_id, outcome="landed for the first time")

    assert receipt["already_promoted"] is False
    assert not pointer_path(run_id).exists()
    rows = ledger.runs(PROJECT, root=repository)
    assert len(rows) == 1
    assert receipt["plan_comment"]["recorded"] is True
    assert "finished_cleanup" not in receipt["plan_comment"]


# ── Case 4: a differing narrative before any row exists ─────────────────────


def test_a_differing_narrative_without_a_row_is_still_refused(
    repository: Path,
) -> None:
    """No committed row means no promotion to defer to, so the refusal stands."""
    run_id = "r-differing-narrative-without-row"
    _pointer(repository, run_id)
    # The comment an interrupted attempt wrote, and the landing commit that
    # would have followed it failed: the plan holds one narrative, no row.
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="s1",
        run_id=run_id,
        narrative="the attempt whose landing never committed",
        author="reckon-build",
        when="2026-10-01T06:00:00Z",
        root=repository,
        worker_tree=repository,
    )
    assert ledger.runs(PROJECT, root=repository) == []

    with pytest.raises(CrewError, match="different narrative"):
        _promote(repository, run_id, outcome="a narrative the plan does not hold")

    # The refusal writes nothing: no row, and the pointer survives for a retry
    # that repeats the narrative the plan already carries.
    assert ledger.runs(PROJECT, root=repository) == []
    assert pointer_path(run_id).exists()


# ── Case 5: what an unreadable or disagreeing ledger answers ────────────────


def test_an_unreadable_ledger_is_not_proof_of_a_written_row(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deciding caller answers False; the receipt still answers present."""

    def refuse(*_args, **_kwargs):
        raise ledger.LedgerError("the aggregate cannot be read")

    monkeypatch.setattr(ledger, "load_aggregate", refuse)

    assert (
        promotion_scope._ledger_holds_row(
            PROJECT, repository, "r-unreadable", unreadable=False
        )
        is False
    )
    assert (
        promotion_scope._ledger_holds_row(PROJECT, repository, "r-unreadable") is True
    )


def test_a_row_the_aggregate_carries_alone_is_proof(
    repository: Path,
) -> None:
    """Either source carries the run's record, so either one proves the row."""
    landed = "r-promoted-with-a-document"
    _pointer(repository, landed)
    _promote(repository, landed, outcome="landed the ordinary way")

    # A promoted run lives in its own document, and the aggregate names no row
    # for it; that document alone proves the row.
    aggregate, version = ledger.load_aggregate(PROJECT, root=repository)
    assert not any(str(row.get("run_id")) == landed for row in aggregate["runs"])
    assert (
        promotion_scope._ledger_holds_row(PROJECT, repository, landed, unreadable=False)
        is True
    )

    # An aggregate row with no document beside it is the same record held in
    # one place, and the promotion path reads it as the run's record.
    carried = "r-aggregate-without-document"
    data = dict(aggregate)
    data["runs"] = [*aggregate["runs"], {"run_id": carried, "gate": "not-run"}]
    ledger.write(PROJECT, data, version, root=repository)
    assert not ledger.run_path(PROJECT, carried, repository).exists()

    assert (
        promotion_scope._ledger_holds_row(
            PROJECT, repository, carried, unreadable=False
        )
        is True
    )
    assert promotion_scope._ledger_holds_row(PROJECT, repository, carried) is True


def test_an_aggregate_only_row_finishes_a_differing_narrative(
    repository: Path,
) -> None:
    """A row the aggregate carries alone defers to no second refusal."""
    run_id = "r-aggregate-only-narrative"
    recorded = "the account the aggregate row was written with"
    retried = "a later account of the same landing"
    _pointer(repository, run_id)
    promotion._record_landing_comment(
        project=PROJECT,
        plan=PLAN,
        section="s1",
        run_id=run_id,
        narrative=recorded,
        author="reckon-build",
        when="2026-10-01T06:00:00Z",
        root=repository,
        worker_tree=repository,
    )
    # The row the interrupted promotion committed, with no document beside it.
    aggregate, version = ledger.load_aggregate(PROJECT, root=repository)
    data = dict(aggregate)
    data["runs"] = [*aggregate["runs"], {"run_id": run_id, "gate": "not-run"}]
    ledger.write(PROJECT, data, version, root=repository)
    assert not ledger.run_path(PROJECT, run_id, repository).exists()

    second = _promote(repository, run_id, outcome=retried)

    assert second["already_promoted"] is True
    assert not pointer_path(run_id).exists()
    comment = second["plan_comment"]
    assert comment["reason"] == "different_narrative"
    assert comment["finished_cleanup"] is True
    assert comment["kept_narrative"] == recorded
    assert any(recorded in body for body in _landing_comment_bodies(repository))


# ── Case 6: an entity the narrative names survives as written ───────────────


def test_a_named_entity_survives_in_the_receipt(
    repository: Path,
) -> None:
    """The report unescapes once, so a literal ``&lt;`` stays a literal."""
    run_id = "r-second-complete-named-entity"
    recorded = "confirmed a &lt; b holds for every case"
    _pointer(repository, run_id)
    _promote(repository, run_id, outcome=recorded)
    _pointer(repository, run_id)

    second = _promote(repository, run_id, outcome="a different account entirely")

    comment = second["plan_comment"]
    assert comment["reason"] == "different_narrative"
    assert "&lt;" in comment["kept_narrative"]
    assert "< b" not in comment["kept_narrative"]
    assert any("&amp;lt;" in body for body in _landing_comment_bodies(repository))
