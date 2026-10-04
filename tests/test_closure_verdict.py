"""A synthesized record's verdict reads each section's final implementing run.

Reading every ledger row makes a plan that was reviewed unable to read as a
pass at all, because a review records its gate as not-run by design. The
verdict therefore reads the population a section attempt count uses —
implement- and test-role runs only — and within a section the latest such run
decides: a pass reads pass, a failure classified pre-existing-failure reads
qualified, and any other failed run reads fail. The record takes the worst
reading across sections.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from reckon import _plan_html, ledger
from reckon.evidence import synthesize_landed_record

PROJECT = "proj"
PLAN = "section-verdict"


def _write_plan(docs: Path) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<title>Section verdict</title></head><body><main class="plan-doc">'
        '<h2 id="delivery">Delivery</h2>'
        '<h2 id="verification">Verification</h2>'
        "</main></body></html>"
    )
    state = {
        "type": "plan",
        "slug": PLAN,
        "title": "Section Verdict",
        "status": "active",
    }
    path = docs / "plans" / f"{PLAN}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")
    return path


def _run(
    run_id: str,
    *,
    role: str,
    section: str,
    gate: str,
    completed_at: str,
    classification: str = "",
) -> dict:
    return ledger.build_record(
        run_id=run_id,
        plan=PLAN,
        section=section,
        role=role,
        node=run_id,
        gate=gate,
        failure_classification=classification,
        completed_at=completed_at,
        completed_at_source="provided",
        worker_seconds=60,
        tests_added=1,
        changed_lines={"insertions": 5, "deletions": 2},
    )


def _delivery(*, superseded_failure: bool) -> list[dict]:
    """Delivery rows: an optional failure a later pass supersedes, then the
    bookend roles, whose not-run gates must never decide the section."""
    rows: list[dict] = []
    if superseded_failure:
        rows.append(
            _run(
                "delivery-early-failure",
                role="implement",
                section="delivery",
                gate="failed",
                classification="work-rejected",
                completed_at="2026-08-24T19:01:00Z",
            )
        )
    rows.append(
        _run(
            "delivery-final-pass",
            role="implement",
            section="delivery",
            gate="passed",
            completed_at="2026-08-24T19:02:00Z",
        )
    )
    rows.append(
        _run(
            "delivery-review",
            role="review",
            section="delivery",
            gate="not-run",
            completed_at="2026-08-24T19:03:00Z",
        )
    )
    rows.append(
        _run(
            "delivery-investigation",
            role="investigate",
            section="delivery",
            gate="not-run",
            completed_at="2026-08-24T19:04:00Z",
        )
    )
    return rows


def _verification(gate: str, classification: str = "") -> list[dict]:
    return [
        _run(
            "verification-run",
            role="implement",
            section="verification",
            gate=gate,
            classification=classification,
            completed_at="2026-08-24T19:05:00Z",
        )
    ]


def _repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
) -> Path:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    root = tmp_path / "repo"
    docs = root / "docs"
    (docs / "state" / PROJECT).mkdir(parents=True)
    ledger.write(PROJECT, {"members": [], "runs": [], "holds": []}, 0, root=root)
    _write_plan(docs)
    for row in rows:
        ledger.append_run(PROJECT, row, root=root)
    return root


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            _delivery(superseded_failure=True)
            + _verification("failed", "pre-existing-failure"),
            "qualified",
            id="a-failure-classed-pre-existing-reads-qualified",
        ),
        pytest.param(
            _delivery(superseded_failure=True)
            + _verification("failed", "work-rejected"),
            "fail",
            id="an-unexcused-failure-reads-fail",
        ),
        pytest.param(
            _delivery(superseded_failure=False) + _verification("passed"),
            "pass",
            id="all-passing-implementing-runs-read-pass",
        ),
    ],
)
def test_closure_verdict_reads_each_sections_final_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict],
    expected: str,
) -> None:
    root = _repository(tmp_path, monkeypatch, rows)

    result = synthesize_landed_record(root / "docs", PROJECT, PLAN)
    output = result.path.read_text(encoding="utf-8")

    assert result.runs == len(rows)
    match = re.search(r'<meta name="plan-verdict" content="([^"]*)">', output)
    assert match is not None, output
    assert match.group(1) == expected
