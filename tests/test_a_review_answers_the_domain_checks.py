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

from reckon import ledger
from reckon.crew import domain_rules
from reckon.crew import recovery_review_subject as subject_module
from reckon.crew import review as review_module
from reckon.crew import runs as runs_module
from reckon.crew.review_lifecycle import OPEN, lifecycle

REVIEWED_RUN = "r-fixture-reviewed"
REVIEW_RUN = "r-fixture-review"

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


def _delivered_record(raw_text: str) -> dict[str, object]:
    """A review record as a reviewer writes it: flat JSON, keyed to its run."""
    return {
        "project": "fixture",
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": "a" * 40,
        "reviewed_head_sha": "b" * 40,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "raw_text": raw_text,
    }


@pytest.fixture()
def reviewed_run_pointer() -> str:
    """The reviewed run's live pointer, carrying the node whose writes were declared.

    The delivery contract resolves a record's declared checks from the reviewed
    run the record names, so the pointer has to be readable for a checklist to
    exist at all. The path is the code's own, never a restatement of it.
    """
    live = runs_module.live_dir()
    live.mkdir(parents=True, exist_ok=True)
    (live / f"{REVIEWED_RUN}.json").write_text(
        json.dumps(
            {
                "run_id": REVIEWED_RUN,
                "project": "fixture",
                "node": _run_record(["models/tape.py"])["node"],
            }
        ),
        encoding="utf-8",
    )
    return REVIEWED_RUN


def test_a_delivered_record_that_skips_a_declared_check_is_refused(
    fixture_project, reviewed_run_pointer
) -> None:
    """The guard runs on the delivered record, not only where a test parses one.

    The parser is called with the text a reviewer emitted, so it holds no record
    and cannot know which checks the review owed. The delivery contract does
    hold the record, and the reviewer runs it against every written record
    before ending, so a skipped check is caught there.
    """
    fixture_project()
    owed = _owed_checks(
        subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    )
    refusals = review_module.review_record_refusals(_delivered_record(_emission({})))

    assert owed == ["same-operator", "perturbation-grows"]
    assert [refusal for refusal in refusals if owed[0] in refusal]


def test_a_delivered_record_answering_every_declared_check_is_not_refused_for_them(
    fixture_project, reviewed_run_pointer
) -> None:
    """An answered check is read from the emission, where the answer survives."""
    fixture_project()
    owed = _owed_checks(
        subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    )
    text = _emission(dict.fromkeys(owed, "pass — read the diff"))

    refusals = review_module.review_record_refusals(_delivered_record(text))

    assert [refusal for refusal in refusals if "declared check" in refusal] == []


def test_a_promoted_subject_whose_review_skips_a_check_is_still_refused(
    fixture_project,
) -> None:
    """The guard survives promotion, when the live pointer is gone.

    A run's pointer is reclaimed the moment it is promoted, which is also when
    its review is filed. Resolving the checklist from the pointer alone would
    switch the guard off at exactly that moment, so the committed per-run row
    stands in: it carries the node definition the run was dispatched from.
    """
    fixture_project()
    owed = _owed_checks(
        subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    )
    row = ledger.run_path("fixture", REVIEWED_RUN)
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(
        json.dumps(
            {"node_definition": {"id": "n1", "write_paths": ["models/tape.py"]}}
        ),
        encoding="utf-8",
    )
    assert not runs_module.pointer_path(REVIEWED_RUN).exists()

    refusals = review_module.review_record_refusals(_delivered_record(_emission({})))

    assert owed == ["same-operator", "perturbation-grows"]
    assert [refusal for refusal in refusals if owed[0] in refusal]


def test_a_subject_with_no_readable_record_is_refused_for_that(
    fixture_project,
) -> None:
    """An unreadable subject is refused, not read as owing no checks.

    "No checks owed" and "cannot tell which checks are owed" read the same from
    the outside, and only one of them is a review that answered its checklist.
    Nothing here is readable: no live pointer, no committed row, no run
    directory.
    """
    fixture_project()
    record = _delivered_record(_emission({}))
    record["reviewed_run_id"] = "r-fixture-promoted-and-reclaimed"

    refusals = review_module.review_record_refusals(record)

    assert [refusal for refusal in refusals if "cannot be established" in refusal]


def test_the_owner_answers_whether_a_project_declares_any_check(
    fixture_project,
) -> None:
    """The project-level question is asked of the module that owns the match.

    One module answers which rules a repository declares, so the check a brief
    names, the verdict a parser requires and this question are one read rather
    than three.
    """
    fixture_project()
    assert domain_rules.declares_review_checks("fixture") is True

    fixture_project(NO_DOMAIN_FLIGHT)
    assert domain_rules.declares_review_checks("fixture") is False


def test_a_promoted_subject_reads_its_delivered_manifest(
    fixture_project,
) -> None:
    """The run's own delivered manifest is the last source, and it is read.

    The manifest names the write paths the run's checks were matched against, so
    it still answers the checklist when the live pointer is reclaimed and no
    committed row remains. Reading it through the module's manifest-field reader
    is what makes the field the same field the rest of the contract reads.
    """
    fixture_project()
    owed = _owed_checks(
        subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    )
    directory = runs_module.run_dir(REVIEWED_RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / review_module._MANIFEST_FILE_NAME).write_text(
        'orientation_write_paths: ["models/tape.py"]\n',
        encoding="utf-8",
    )
    assert not runs_module.pointer_path(REVIEWED_RUN).exists()
    assert not ledger.run_path("fixture", REVIEWED_RUN).exists()

    refusals = review_module.review_record_refusals(_delivered_record(_emission({})))

    assert owed
    assert [refusal for refusal in refusals if owed[0] in refusal]


def test_a_project_without_declarations_is_unaffected_by_the_contract(
    fixture_project, reviewed_run_pointer
) -> None:
    """The control: a readable subject that owes nothing is still not refused."""
    fixture_project(NO_DOMAIN_FLIGHT)

    refusals = review_module.review_record_refusals(_delivered_record(_emission({})))

    assert [refusal for refusal in refusals if "declared check" in refusal] == []
    assert [refusal for refusal in refusals if "cannot be established" in refusal] == []


def test_a_project_without_declarations_is_unchanged(fixture_project) -> None:
    """The control: no declaration, no checklist, and the floor still stands."""
    fixture_project(NO_DOMAIN_FLIGHT)
    brief = subject_module._review_brief_text(_run_record(["models/tape.py"]), "a" * 40)
    assert subject_module.DECLARED_CHECK_HEADING not in brief

    record = review_module.parse_review(_emission({}))
    assert record["status"] == "parsed"
    assert record["absent_items"] == []
    assert len(record["scores"]) == len(review_module.REVIEW_DIMENSIONS) == 6
