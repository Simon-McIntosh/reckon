"""A review answers the domain checks its node touches, or says it did not.

A repository declares its domain rules once and one module matches them to a
node's write paths. The review checks that match a node join the review's
checklist rather than a scored dimension: the composed brief names each one, the
parser requires an answered verdict for each, and a check answered ``fail`` is
stored as a blocking finding so the lifecycle derivation that already reads
severities sees it. The dimension vocabulary, the acceptance floor and the
lifecycle module are untouched; the last test is the control that says so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery_review_subject as subject_module
from reckon.crew import review as review_module
from reckon.crew.recovery_review_acceptance import REVIEW_ACCEPTANCE_FLOOR
from reckon.crew.review_lifecycle import OPEN, lifecycle

FLIGHT = """\
domain:
  review_checks:
    - id: same-operator
      question: Do both sides of the comparison pass through the same measurement operator?
      applies_to: ["models/**"]
    - id: perturbation-grows
      question: Does a small perturbation grow in a lossy system?
      applies_to: ["models/**"]
    - id: dc-limit
      question: Is the limit behaviour stated at DC and at high frequency?
      applies_to: ["lib/**"]
"""

NO_DOMAIN_FLIGHT = """\
gates:
  dimension_floors: {}
"""


@pytest.fixture()
def fixture_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mounted project whose declared checks are readable without an install."""

    def _mount(text: str = FLIGHT) -> str:
        docs = tmp_path / "docs"
        (docs / "state" / "fixture").mkdir(parents=True, exist_ok=True)
        (docs / "state" / "fixture" / "flight.yaml").write_text(text, encoding="utf-8")
        mounts = tmp_path / "mounts.json"
        mounts.write_text(json.dumps({"fixture": str(docs)}), encoding="utf-8")
        monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
        # The host layer is this workstation's own config unless it is pointed
        # away, and a rule declared there would reach this fixture's match.
        monkeypatch.setenv(
            "RECKON_FLIGHT_CONFIG", str(tmp_path / "absent" / "flight.yaml")
        )
        return str(docs)

    return _mount


def _run_record(write_paths: list[str]) -> dict[str, object]:
    """A reviewed run's record, carrying the node whose writes were declared."""
    return {
        # A node carrying a plan is reviewed as a diff, which keeps the brief
        # composer off the fleet's own run directories.
        "node": {"id": "n1", "write_paths": write_paths, "plan": "a-plan"},
        "project": "fixture",
        "run_id": "r-fixture",
    }


def _owed_checks(brief: str) -> list[str]:
    """The check ids the composed brief tells its reviewer to answer.

    Read from the brief rather than from the review record, because the brief is
    what the reviewer is handed: a check the brief does not name cannot be owed,
    and one it names is owed whether or not the reviewer answers it. A brief
    carrying no block owes nothing, which is what the control reads.
    """
    lines = brief.splitlines()
    heading = subject_module.DECLARED_CHECK_HEADING + ":"
    if heading not in lines:
        return []
    start = lines.index(heading)
    owed: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        entry = line.strip()
        if entry.upper().startswith("VERDICT "):
            continue
        owed.append(entry.split(":", 1)[0].strip())
    return owed


def _emission(verdicts: dict[str, str]) -> str:
    """A complete emitted review, with one line per declared check."""
    lines = [f"SCORE {dimension}: 18" for dimension in review_module.REVIEW_DIMENSIONS]
    lines.extend(
        f"VERDICT {item}: examined {item}" for item in review_module.REVIEW_ITEMS
    )
    lines.extend(f"VERDICT {check}: {verdict}" for check, verdict in verdicts.items())
    return "\n".join(lines)


def test_the_composed_brief_lists_exactly_the_checks_that_match(
    fixture_project,
) -> None:
    """A node's brief carries the checks it touches, and no others."""
    fixture_project()
    brief = subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)

    owed = _owed_checks(brief)
    assert owed == ["same-operator", "perturbation-grows"]
    assert "dc-limit" not in brief


def test_a_node_writing_elsewhere_carries_no_check(fixture_project) -> None:
    """A node matching none of the declared globs owes no check."""
    fixture_project()
    brief = subject_module._review_brief_text(_run_record(["docs/notes.md"]), "a" * 40)

    assert subject_module.DECLARED_CHECK_HEADING not in brief
    assert _owed_checks(brief) == []


def test_a_record_with_every_verdict_parses_complete(fixture_project) -> None:
    fixture_project()
    brief = subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    owed = _owed_checks(brief)

    record = review_module.parse_review(
        _emission(dict.fromkeys(owed, "pass — read the diff")),
        review_checks=owed,
    )

    assert record["status"] == "parsed"
    assert record["absent_items"] == []
    assert record["item_aggregate"] == len(review_module.REVIEW_ITEMS) + len(owed)


def test_a_record_missing_a_verdict_parses_incomplete_naming_the_check(
    fixture_project,
) -> None:
    """A check the reviewer skipped is named, and the record is not a pass."""
    fixture_project()
    brief = subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    owed = _owed_checks(brief)
    answered = dict.fromkeys(owed[:-1], "pass — read the diff")

    record = review_module.parse_review(_emission(answered), review_checks=owed)

    assert record["status"] == "incomplete"
    assert owed[-1] in record["absent_items"]
    assert record["item_aggregate"] is None


def test_a_failed_check_is_stored_blocking_and_the_run_reads_open(
    fixture_project,
) -> None:
    """A failed check holds the node open through the existing derivation."""
    fixture_project()
    owed = _owed_checks(
        subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    )
    verdicts = dict.fromkeys(owed, "pass — read the diff")
    verdicts[owed[0]] = "fail — both sides pass through a different filter"

    record = review_module.parse_review(_emission(verdicts), review_checks=owed)
    stored = dict(record, reviewed_run_id="r-fixture", project="fixture")

    failures = [
        finding
        for finding in record["findings"]
        if finding.get("severity") == review_module.BLOCKING_FINDING_SEVERITY
    ]
    assert record["status"] == "parsed"
    assert len(failures) == 1
    assert owed[0] in failures[0]["text"]
    assert lifecycle(stored) == OPEN


def test_a_project_without_declarations_is_unchanged(fixture_project) -> None:
    """The control: no declaration, no checklist, and the floor still stands."""
    fixture_project(NO_DOMAIN_FLIGHT)
    brief = subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    assert subject_module.DECLARED_CHECK_HEADING not in brief

    record = review_module.parse_review(_emission({}))
    assert record["status"] == "parsed"
    assert record["absent_items"] == []
    assert len(record["scores"]) == len(review_module.REVIEW_DIMENSIONS) == 6
    # Six dimensions at REVIEW_MAX_SCORE each, nine tenths of the total.
    assert REVIEW_ACCEPTANCE_FLOOR == 6 * review_module.REVIEW_MAX_SCORE * 9 // 10
