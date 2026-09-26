"""Composition of a plan's cumulative evidence record at read time.

Two cases stand against the one composition function: the folding case, where a
record with fragments composes to the record's bytes followed by its fragments
in ledger promotion order, and the identity case, where a record with no
fragments composes to its own bytes unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.evidence import compose_landed_record

PROJECT = "proj"
PLAN = "composed-plan"
OTHER_PLAN = "other-plan"

RECORD_BYTES = (
    b'<!doctype html>\n<html lang="en"><head>\n'
    b'  <meta charset="utf-8">\n'
    b'  <meta name="docs-project" content="proj">\n'
    b'  <meta name="reckon-type" content="evidence">\n'
    b'  <meta name="plan-evidence-for" content="composed-plan">\n'
    b'</head><body><main class="plan-doc"><h1>Record</h1></main></body></html>\n'
)

# Promotion order is zeta, mid, alpha (by completion time); filename order is
# alpha, mid, zeta — a permutation no filename sort can produce, so the two
# orderings cannot be confused for one another.
NODES_BY_PROMOTION = ("zeta-fragment", "mid-fragment", "alpha-fragment")
NODES_BY_FILENAME = ("alpha-fragment", "mid-fragment", "zeta-fragment")


def _fragment_bytes(node: str) -> bytes:
    return (
        f'<article class="landed-fragment" data-node="{node}">{node} anchor</article>\n'
    ).encode()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)

    fragment_dir = docs / "evidence" / "fragments" / PLAN
    fragment_dir.mkdir(parents=True)
    for node in NODES_BY_PROMOTION:
        (fragment_dir / f"{node}.html").write_bytes(_fragment_bytes(node))

    for index, node in enumerate(NODES_BY_PROMOTION):
        ledger.append_run(
            PROJECT,
            ledger.build_record(
                run_id=f"run-{node}",
                plan=PLAN,
                section="delivery",
                node=node,
                gate="passed",
                completed_at=f"2026-08-24T19:0{index + 1}:00Z",
                completed_at_source="provided",
            ),
            root=root,
        )
    return root


@pytest.mark.parametrize("spelling", ["archive", "live"])
def test_record_composes_to_its_bytes_then_fragments_in_promotion_order(
    repository: Path, spelling: str
) -> None:
    evidence_dir = repository / "docs" / "evidence"
    if spelling == "archive":
        record_path = evidence_dir / "archive" / f"{PLAN}-landed.html"
    else:
        record_path = evidence_dir / f"{PLAN}-landed.html"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(RECORD_BYTES)

    composed = compose_landed_record(record_path, PLAN, project=PROJECT)

    expected = RECORD_BYTES + b"".join(
        _fragment_bytes(node) for node in NODES_BY_PROMOTION
    )
    assert composed == expected

    # The declared negative control: ordering by filename rather than by ledger
    # promotion order composes to different bytes, so the ordering assertion
    # above can fail.
    filename_order = RECORD_BYTES + b"".join(
        _fragment_bytes(node) for node in NODES_BY_FILENAME
    )
    assert composed != filename_order


def test_records_with_no_fragments_compose_to_their_own_bytes() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence_dir = root / "docs" / "evidence"
    records = sorted(evidence_dir.glob("*-landed.html")) + sorted(
        (evidence_dir / "archive").glob("*-landed.html")
    )

    # The check set is derived by the same rule the assertion is about — a
    # record with no fragments — rather than pinned to the tree's current
    # count, so a fragment landing under another plan does not turn this test
    # red. The count checked is reported, not asserted against the total.
    def _fragment_dir(record_path: Path) -> Path:
        return evidence_dir / "fragments" / record_path.name[: -len("-landed.html")]

    without_fragments = [
        record_path
        for record_path in records
        if not any(_fragment_dir(record_path).glob("*.html"))
    ]

    assert without_fragments, "every record has fragments; nothing was checked"

    for record_path in without_fragments:
        plan_slug = record_path.name[: -len("-landed.html")]
        project = str(_plan_html.parse_plan(record_path).get("project") or "reckon")
        assert (
            compose_landed_record(record_path, plan_slug, project=project)
            == record_path.read_bytes()
        ), record_path


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    return root, docs


def _fragment(docs: Path, plan: str, node: str) -> Path:
    fragment_dir = docs / "evidence" / "fragments" / plan
    fragment_dir.mkdir(parents=True, exist_ok=True)
    fragment = fragment_dir / f"{node}.html"
    fragment.write_bytes(_fragment_bytes(node))
    return fragment


def _promote(root: Path, plan: str, node: str, run_id: str, completed_at: str) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id=run_id,
            plan=plan,
            section="delivery",
            node=node,
            gate="passed",
            completed_at=completed_at,
            completed_at_source="provided",
        ),
        root=root,
    )


def _record(docs: Path, plan: str) -> Path:
    archive = docs / "evidence" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    record_path = archive / f"{plan}-landed.html"
    record_path.write_bytes(RECORD_BYTES)
    return record_path


def test_a_redispatched_node_contributes_its_fragment_once_at_its_earliest_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docs = _seed(tmp_path, monkeypatch)
    _fragment(docs, PLAN, "dup-node")
    _fragment(docs, PLAN, "mid-node")
    _promote(root, PLAN, "dup-node", "run-dup-1", "2026-08-24T19:01:00Z")
    _promote(root, PLAN, "mid-node", "run-mid", "2026-08-24T19:02:00Z")
    _promote(root, PLAN, "dup-node", "run-dup-2", "2026-08-24T19:03:00Z")

    composed = compose_landed_record(_record(docs, PLAN), PLAN, project=PROJECT)

    # Earliest promotion places the redispatched node before the one promoted
    # between its two promotions.
    assert composed == RECORD_BYTES + b"".join(
        _fragment_bytes(node) for node in ("dup-node", "mid-node")
    )
    # And exactly once: without the dedup the second promotion appends it again.
    assert composed.count(b'data-node="dup-node"') == 1


def test_another_plans_row_does_not_reorder_this_plans_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docs = _seed(tmp_path, monkeypatch)
    _fragment(docs, PLAN, "alpha-fragment")
    _fragment(docs, PLAN, "beta-fragment")
    _promote(root, PLAN, "beta-fragment", "run-beta", "2026-08-24T19:02:00Z")
    _promote(root, PLAN, "alpha-fragment", "run-alpha", "2026-08-24T19:03:00Z")
    # Another plan promotes a node of the same name earlier than this plan does.
    _promote(root, OTHER_PLAN, "alpha-fragment", "run-other", "2026-08-24T19:01:00Z")

    composed = compose_landed_record(_record(docs, PLAN), PLAN, project=PROJECT)

    # This plan's own promotion order is beta then alpha; the other plan's row
    # must not pull alpha forward. Removing the plan filter would compose
    # alpha, beta instead.
    assert composed == RECORD_BYTES + b"".join(
        _fragment_bytes(node) for node in ("beta-fragment", "alpha-fragment")
    )
    assert composed != RECORD_BYTES + b"".join(
        _fragment_bytes(node) for node in ("alpha-fragment", "beta-fragment")
    )


def test_a_fragment_with_no_promotion_composes_after_the_ledgered_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, docs = _seed(tmp_path, monkeypatch)
    _fragment(docs, PLAN, "beta-fragment")
    _fragment(docs, PLAN, "alpha-fragment")
    _fragment(docs, PLAN, "gamma-fragment")
    _promote(root, PLAN, "beta-fragment", "run-beta", "2026-08-24T19:02:00Z")

    composed = compose_landed_record(_record(docs, PLAN), PLAN, project=PROJECT)

    # The ledgered fragment first, then the two merged-but-unpromoted ones in
    # filename order, so nothing on disk is hidden.
    assert composed == RECORD_BYTES + b"".join(
        _fragment_bytes(node)
        for node in ("beta-fragment", "alpha-fragment", "gamma-fragment")
    )
