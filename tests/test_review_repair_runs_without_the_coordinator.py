"""A stored finding carries the id an answer names, from the moment it is stored.

The reflex answers a stored finding by identity: a repair says which finding it
acted on, and the lifecycle reads that answer back. A finding stored without an
id can be answered by nothing, so this file pins the identity to the record on
disk rather than to a reader that re-derives it. The id is the shared
``repair.finding_id`` function of the finding's own file, line and text, so the
stored id and the id a repair brief prints are one value.

The round cap, the escalation and the re-review brief this node's plan also
describes live in modules outside this node's write grant; those arms are
recorded as a blocker in the run manifest rather than exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import review_lifecycle

PROJECT = "sample"
RUN_ID = "r-reviewed"
FINDING = {
    "file": "reckon/crew/thing.py",
    "line": "10",
    "text": "off-by-one in the loop",
}


def _record(findings: list[dict]) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN_ID,
        "status": "parsed",
        "scores": dict(zip(review_module.REVIEW_DIMENSIONS, (10,) * 6, strict=True)),
        "total": 60,
        "findings": findings,
    }


def _store(tmp_path: Path, monkeypatch, findings: list[dict]) -> dict:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    path = review_module.store_review(_record(findings))
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_stored_review_s_findings_carry_the_shared_id(tmp_path, monkeypatch) -> None:
    """The record on disk carries the id ``repair.finding_id`` derives for it."""
    stored = _store(tmp_path, monkeypatch, [dict(FINDING)])

    findings = stored["findings"]
    assert len(findings) == 1
    expected = repair_module.finding_id(FINDING)
    assert findings[0]["id"] == expected
    # The identity is not positional: it derives from the finding's own content.
    assert expected.startswith(repair_module.FINDING_ID_PREFIX)


def test_a_declined_finding_is_answerable_by_its_stored_id(
    tmp_path, monkeypatch
) -> None:
    """An answered stored finding leaves the lifecycle's open set.

    Without the stored id the lifecycle can neither match nor be given the
    answer, so this is the behaviour the stamped id enables.
    """
    stored = _store(tmp_path, monkeypatch, [dict(FINDING)])
    stored["responses"] = {stored["findings"][0]["id"]: {"action": "declined"}}

    assert review_lifecycle._run_open(stored) is False
