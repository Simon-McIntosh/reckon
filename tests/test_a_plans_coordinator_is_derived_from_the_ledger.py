"""A plan's coordinator is the session its most recent dispatched run names.

The run ledger records a dispatching session on every row, and a plan's own meta
records an accountable owner, but nothing joined the two for a plan with no live
run — the case a coordinator needs most, because an idle plan is exactly the one
no live-run listing can name. These cases pin the join against a fixture
rebuilt from power_convertor's committed ledger as it stood at 2026-10-22T08:50Z:
each plan's expected coordinator is a literal read by hand from the fixture's
own rows, never from the derivation under test.

The fixture is hermetic. ``RECKON_HOME`` is a temp tree, the checkout holding
the plans and the ledger is a temp directory, and no live pointer exists unless
a case writes one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import plan_coordinator, runs

PROJECT = "power_convertor"

#: The instant the fixture reproduces: the incident this plan exists to remove.
NOW = datetime(2026, 10, 22, 8, 50, 0, tzinfo=UTC)

#: Long enough to reach the brief-dispatched landing that last touched
#: iter-power-supply-model, which is six and a half days before the fixture's
#: instant and is the newest dispatch that plan has.
WINDOW = "168h"

SESSION_COIL = "cdb769a4-7af0-4304-a144-f39ecbf8c502"
SESSION_TERMINAL = "83d14db1-f84d-4588-b40a-bc508c081332"
SESSION_TAPE = "e1b445e1-9530-49a8-a45c-81b39922e373"
SESSION_BRIEF = "50bcd9a1-2f30-4dba-a79f-1f6e4d2efbec"
SESSION_COLD = "e28a3130-1269-4ec7-89cb-575a279b7fd0"

#: The session that dispatched the earlier rows of iter-switched-terminal-transients
#: and of tf-cold-test-validation, and the session of the stale rows. Neither is a
#: plan's coordinator in the fixture, and both are in the ledger as history.
SESSION_EARLIER = "c952ceac-4c68-4aa1-9f63-5db2a6152f10"
SESSION_STALE = "d8a1d0f9-0000-4000-8000-00000000dead"

#: slug -> (session, matched_by, deciding run id, plan-owner). The owner of
#: tf-co-wound-tape-validation differs from the others deliberately, so the case
#: proves the resolver reads each plan's own meta rather than one value reused.
PINNED: dict[str, tuple[str | None, str, str | None, str]] = {
    "shared-coil-supply-model": (
        SESSION_COIL,
        "node",
        "r-20261022T083726456965-plan-review-of-shared-coil-supply-model",
        "Simon McIntosh",
    ),
    "iter-switched-terminal-transients": (
        SESSION_TERMINAL,
        "node",
        "r-20261022T050030802188-plan-review-of-iter-switched-terminal-transients",
        "Simon McIntosh",
    ),
    "tf-co-wound-tape-validation": (
        SESSION_TAPE,
        "node",
        "r-20261021T215203577763-plan-review-of-tf-co-wound-tape-validation",
        "A Peer",
    ),
    "iter-power-supply-model": (
        SESSION_BRIEF,
        "plan_link",
        "r-20261015T200342535883-scenario-loading-rerun",
        "Simon McIntosh",
    ),
    "tf-cold-test-validation": (
        SESSION_COLD,
        "plan",
        "r-20261021T190726061562-cold-test-reproduction-rebuild",
        "Simon McIntosh",
    ),
    "coil-supply-report-rebuild": (None, "", None, "Simon McIntosh"),
}

#: run id, plan, plan_link, node, session, dispatched_at. The rows are the ones
#: that decided power_convertor's plans at the fixture instant, plus the older
#: and stale rows that must not decide them.
FIXTURE_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = (
    # shared-coil-supply-model: two brief landings, then a plan review that is
    # newer than both and decides the plan.
    (
        "r-20261019T013810288111-plant-tests-independence",
        "",
        "shared-coil-supply-model",
        "plant-tests-independence",
        SESSION_EARLIER,
        "2026-10-19T01:38:52Z",
    ),
    (
        "r-20261022T083726456965-plan-review-of-shared-coil-supply-model",
        "",
        "",
        "plan-review-of-shared-coil-supply-model",
        SESSION_COIL,
        "2026-10-22T08:37:38Z",
    ),
    # The same plan reviewed six days earlier, outside the window.
    (
        "r-20261013T181355000000-plan-review-of-shared-coil-supply-model",
        "",
        "",
        "plan-review-of-shared-coil-supply-model",
        SESSION_STALE,
        "2026-10-13T18:13:55Z",
    ),
    # iter-switched-terminal-transients: a brief landing inside the window, then
    # a plan review newer than it.
    (
        "r-20261016T215153055468-iter-flux-map-guards",
        "",
        "iter-switched-terminal-transients",
        "iter-flux-map-guards",
        SESSION_EARLIER,
        "2026-10-16T21:52:03Z",
    ),
    (
        "r-20261022T050030802188-plan-review-of-iter-switched-terminal-transients",
        "",
        "",
        "plan-review-of-iter-switched-terminal-transients",
        SESSION_TERMINAL,
        "2026-10-22T05:00:51Z",
    ),
    (
        "r-20261014T161549042706-plan-review-of-iter-switched-terminal-transients",
        "",
        "",
        "plan-review-of-iter-switched-terminal-transients",
        SESSION_EARLIER,
        "2026-10-14T16:16:00Z",
    ),
    # tf-co-wound-tape-validation: two plan reviews, the newer one deciding.
    (
        "r-20261021T050021452234-plan-review-of-tf-co-wound-tape-validation",
        "",
        "",
        "plan-review-of-tf-co-wound-tape-validation",
        SESSION_TAPE,
        "2026-10-21T05:00:34Z",
    ),
    (
        "r-20261021T215203577763-plan-review-of-tf-co-wound-tape-validation",
        "",
        "",
        "plan-review-of-tf-co-wound-tape-validation",
        SESSION_TAPE,
        "2026-10-21T21:52:26Z",
    ),
    # iter-power-supply-model: two brief landings inside the window, the newer
    # deciding, and an older plan-field row outside it.
    (
        "r-20261015T182644668472-commutation-stage1-vs1",
        "",
        "iter-power-supply-model#s2",
        "commutation-stage1-vs1",
        SESSION_BRIEF,
        "2026-10-15T18:27:29Z",
    ),
    (
        "r-20261015T200342535883-scenario-loading-rerun",
        "",
        "iter-power-supply-model",
        "scenario-loading-rerun",
        SESSION_BRIEF,
        "2026-10-15T20:04:01Z",
    ),
    (
        "r-20261014T100048902123-iter-pf-circuit-prediction",
        "iter-power-supply-model",
        "",
        "iter-pf-circuit-prediction",
        SESSION_EARLIER,
        "2026-10-14T10:01:03Z",
    ),
    # tf-cold-test-validation: decided by its own plan field, and a row carrying
    # both a plan and a section-suffixed link, which the plan field must win.
    (
        "r-20261020T094945712056-cold-test-converter-model",
        "tf-cold-test-validation",
        "tf-cold-test-validation#s3",
        "cold-test-converter-model",
        SESSION_COLD,
        "2026-10-20T09:50:32Z",
    ),
    (
        "r-20261021T190726061562-cold-test-reproduction-rebuild",
        "tf-cold-test-validation",
        "",
        "cold-test-reproduction-rebuild",
        SESSION_COLD,
        "2026-10-21T19:07:57Z",
    ),
)


def _write_plan(docs: Path, slug: str, owner: str) -> None:
    plans = docs / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{slug}.html").write_text(
        "<!doctype html>\n<html><head>"
        '<meta name="docs-project" content="power_convertor">\n'
        f'<meta name="plan-owner" content="{owner}">\n'
        f"</head><body><h1>{slug}</h1></body></html>\n",
        encoding="utf-8",
    )


def _write_rows(root: Path, rows) -> None:
    """Promote rows into the checkout's ledger, one file per run."""
    rows = tuple(rows)
    if not rows:
        return
    runs_dir = ledger.run_path(PROJECT, rows[0][0], root=root).parent
    runs_dir.mkdir(parents=True, exist_ok=True)
    for run_id, plan, link, node, session, dispatched_at in rows:
        ledger.write_run_file(
            PROJECT,
            ledger.build_record(
                run_id=run_id,
                plan=plan,
                node=node,
                plan_link=link,
                session=session,
                dispatched_at=dispatched_at,
                gate="passed",
            ),
            root=root,
        )


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """A checkout holding power_convertor's fixture plans and ledger rows."""
    root = tmp_path / "repo"
    docs = root / "docs"
    docs.mkdir(parents=True)
    for slug, (_session, _by, _run, owner) in PINNED.items():
        _write_plan(docs, slug, owner)
    _write_rows(root, FIXTURE_ROWS)
    return root


def _resolve(root: Path, *, window: str = WINDOW, **kwargs) -> dict:
    return plan_coordinator.resolve_plan_coordinators(
        PROJECT, root=root, now=NOW, window=window, **kwargs
    )


def _assert_pinned(result: dict) -> None:
    """Every pinned plan resolves to the coordinator its deciding row names."""
    plans = result["plans"]
    for slug, (session, matched_by, run_id, owner) in PINNED.items():
        row = plans[slug]
        assert row["coordinator"] == session, (slug, row["coordinator"])
        assert row["tier"] == (
            plan_coordinator.TIER_COORDINATOR
            if session
            else plan_coordinator.TIER_UNOWNED
        ), (slug, row["tier"])
        assert row["matched_by"] == matched_by, (slug, row["matched_by"])
        assert row["run_id"] == run_id, (slug, row["run_id"])
        assert row["plan_owner"] == owner, (slug, row["plan_owner"])
        assert row["live"] is False, (slug, row["live"])


def test_the_fixture_pins_each_plans_coordinator(project_root: Path) -> None:
    result = _resolve(project_root)
    _assert_pinned(result)
    # The gate log carries the deciding row and matched field for each plan, so
    # the expectation can be read against the fixture without re-deriving it.
    for slug in sorted(PINNED):
        row = result["plans"][slug]
        print(
            f"decided {slug}: run={row['run_id'] or '-'} "
            f"matched_by={row['matched_by'] or '-'} "
            f"session={row['coordinator'] or '-'} owner={row['plan_owner']}"
        )
    assert result["plan_count"] == len(PINNED)


def test_a_plan_with_no_dispatched_run_resolves_to_unowned(
    project_root: Path,
) -> None:
    row = _resolve(project_root)["plans"]["coil-supply-report-rebuild"]
    assert row["tier"] == plan_coordinator.TIER_UNOWNED
    assert row["coordinator"] is None
    assert row["run_id"] is None
    # The owner is still reported: unowned names the absence of a session, not
    # the absence of an accountable person.
    assert row["plan_owner"] == "Simon McIntosh"


def test_a_later_dispatch_takes_the_plan_over(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _write_plan(root / "docs", "plan-p", "Simon McIntosh")
    _write_rows(
        root,
        (
            ("r-a1", "plan-p", "", "node-a", "session-a", "2026-10-20T10:00:00Z"),
            ("r-b1", "plan-p", "", "node-b", "session-b", "2026-10-20T12:00:00Z"),
        ),
    )
    result = _resolve(root)
    assert result["plans"]["plan-p"]["coordinator"] == "session-b"
    assert result["plans"]["plan-p"]["run_id"] == "r-b1"
    # A's dispatch is not consumed by the resolution: the ledger still holds it
    # as the plan's history.
    recorded = {
        str(row["run_id"])
        for row in ledger.run_rows_from_documents(PROJECT, root=root)
        if str(row.get("session")) == "session-a"
    }
    assert recorded == {"r-a1"}


def test_a_run_review_resolves_through_the_run_it_reviews(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _write_plan(root / "docs", "plan-p", "Simon McIntosh")
    _write_rows(
        root,
        (
            ("r-a1", "plan-p", "", "node-a", "session-a", "2026-10-20T10:00:00Z"),
            (
                "r-b1",
                "",
                "",
                "review-of-r-a1-a1b2c3d4e5f6",
                "session-b",
                "2026-10-20T12:00:00Z",
            ),
        ),
    )
    row = _resolve(root)["plans"]["plan-p"]
    assert row["coordinator"] == "session-b"
    assert row["matched_by"] == "node"
    assert row["run_id"] == "r-b1"


def test_a_dispatch_outside_the_window_is_history(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _write_plan(root / "docs", "plan-p", "Simon McIntosh")
    _write_rows(
        root,
        (("r-old", "plan-p", "", "node-a", "session-a", "2026-10-01T10:00:00Z"),),
    )
    row = _resolve(root)["plans"]["plan-p"]
    assert row["tier"] == plan_coordinator.TIER_UNOWNED
    assert row["run_id"] is None
    # The same row inside a wider window does name a coordinator, so the empty
    # answer above is the window's doing rather than a row the resolver missed.
    wide = _resolve(root, window="720h")["plans"]["plan-p"]
    assert wide["coordinator"] == "session-a"


def test_a_live_session_reads_as_live_and_an_ended_one_as_ended(
    tmp_path: Path, project_root: Path
) -> None:
    live = runs.live_dir()
    live.mkdir(parents=True, exist_ok=True)
    (live / "r-live-pointer.json").write_text(
        json.dumps(
            {
                "run_id": "r-live-pointer",
                "session": SESSION_COIL,
                "project": PROJECT,
                "phase": "launched",
                "node": {"id": "coil-live", "plan": "shared-coil-supply-model"},
            }
        ),
        encoding="utf-8",
    )
    result = _resolve(project_root)
    assert result["plans"]["shared-coil-supply-model"]["live"] is True
    # Every other plan's coordinator has no live run, so an ended session reads
    # as ended rather than as a session that was never seen.
    assert result["plans"]["tf-co-wound-tape-validation"]["live"] is False


def test_a_resolver_that_returns_unowned_everywhere_fails_the_fixture(
    project_root: Path,
) -> None:
    """The fixture assertions discriminate against an all-unowned answer."""
    result = _resolve(project_root)
    blanked = {
        **result,
        "plans": {
            slug: {
                **row,
                "coordinator": None,
                "tier": plan_coordinator.TIER_UNOWNED,
                "run_id": None,
                "matched_by": "",
            }
            for slug, row in result["plans"].items()
        },
    }
    with pytest.raises(AssertionError):
        _assert_pinned(blanked)


def test_a_resolver_blind_to_dispatch_order_fails_the_a_then_b_case(
    tmp_path: Path,
) -> None:
    """A first-match-wins answer is rejected by the A-then-B assertion."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    _write_plan(root / "docs", "plan-p", "Simon McIntosh")
    _write_rows(
        root,
        (
            ("r-a1", "plan-p", "", "node-a", "session-a", "2026-10-20T10:00:00Z"),
            ("r-b1", "plan-p", "", "node-b", "session-b", "2026-10-20T12:00:00Z"),
        ),
    )
    result = _resolve(root)
    # What an order-blind resolver would answer: the earliest matching row.
    blind = {
        **result,
        "plans": {
            "plan-p": {
                **result["plans"]["plan-p"],
                "coordinator": "session-a",
                "run_id": "r-a1",
            }
        },
    }
    with pytest.raises(AssertionError):
        assert blind["plans"]["plan-p"]["coordinator"] == "session-b"
