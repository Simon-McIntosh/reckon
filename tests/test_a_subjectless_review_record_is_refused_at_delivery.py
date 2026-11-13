"""A review record with no subject is refused where the reviewer can still repair it.

A reviewer may compose its record itself and write the JSON straight into the
record path its dispatch granted, and on 2026-10-18 one did: the record scored
every dimension and named no reviewed run and no plan. ``crew complete`` read
the run as delivered, and promotion then refused the record because it had no
subject to be filed under, so the review could never be stored and its finding
could never be disposed.

Every reviewer passes ``crew check --manifest`` before its turn ends, so the
subject the promotion gate requires is required there too. These cases hold the
delivery check to that: a record that carries every other part of the delivery
contract but names neither a reviewed run nor a plan is refused and named, and
the same record carrying its subject passes unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.review import REVIEW_DIMENSIONS
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir

REVIEWED_RUN_ID = "r-20260923T205045006945-subjectless-reviewed"
REVIEW_RUN_ID = "r-20260924T081500000000-review-of-subjectless"
PROJECT = "nova"
HEAD_SHA = "a" * 40
BASE_SHA = "0" * 40

REVIEW_MANIFEST = """\
node: review-of-{run}
status: complete
messages_received: none
commits: none
changed_paths:
{paths}
tests: record parsed, stored and parse-checked
"""


def _record(subject: dict[str, str]) -> dict:
    """A complete scored review record, carrying whichever subject is given."""
    record = {
        "project": PROJECT,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": BASE_SHA,
        "reviewed_head_sha": HEAD_SHA,
        "status": "parsed",
        "scores": dict.fromkeys(REVIEW_DIMENSIONS, 20),
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
    }
    record.update(subject)
    return record


def _manifest_body(paths: list[str]) -> str:
    return REVIEW_MANIFEST.format(
        run=REVIEW_RUN_ID,
        paths="\n".join(f"  - {path}" for path in paths),
    )


def _check() -> tuple[int, list[str]]:
    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", REVIEW_RUN_ID]
    )
    return result.exit_code, json.loads(result.output)["findings"]


def _deliver() -> tuple[int, str]:
    """Run the delivery gate itself, which is where a record is filed or refused."""
    result = CliRunner().invoke(
        cli_main,
        ["crew", "complete", "--run", REVIEW_RUN_ID, "--gate", "not-run", "--dry-run"],
    )
    return result.exit_code, result.output


@pytest.fixture()
def review_run(isolated_reckon_home: Path, tmp_path: Path) -> dict[str, Path]:
    """A review run delivering one record into the reviewed run's directory.

    Every directory this case resolves is named by the crew home the suite put
    in force, asserted here rather than assumed: a home resolution that stopped
    honouring the substitution would otherwise write a run pointer into the
    production tree this case has no business touching.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)

    record = run_dir(REVIEWED_RUN_ID) / "review-records" / f"{REVIEW_RUN_ID}.json"
    sibling = (
        run_dir(REVIEWED_RUN_ID)
        / "review-records"
        / f"{REVIEW_RUN_ID}.at-{HEAD_SHA[:12]}.json"
    )
    record.parent.mkdir(parents=True, exist_ok=True)

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    manifest = tmp_path / "manifest.md"

    _write_json(
        pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": BASE_SHA,
            "launch": "cli",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{REVIEWED_RUN_ID}",
                "plan": "fixture",
                "section": "s22",
                "role": "review",
                "write_paths": [str(record), str(sibling)],
            },
        },
    )
    return {"record": record, "sibling": sibling, "manifest": manifest}


def test_a_record_naming_no_subject_is_refused_at_delivery(
    review_run: dict[str, Path],
) -> None:
    """The subject promotion requires is required before the turn ends."""
    record = review_run["record"]
    record.write_text(json.dumps(_record({})), encoding="utf-8")
    review_run["manifest"].write_text(_manifest_body([str(record)]), encoding="utf-8")

    exit_code, findings = _check()

    assert exit_code != 0, findings
    assert any("no subject" in finding for finding in findings), findings
    assert all(str(record) in finding for finding in findings), findings


def test_a_record_naming_its_reviewed_run_passes(review_run: dict[str, Path]) -> None:
    """The same record carrying its subject is unchanged by the check."""
    record = review_run["record"]
    record.write_text(
        json.dumps(_record({"reviewed_run_id": REVIEWED_RUN_ID})), encoding="utf-8"
    )
    review_run["manifest"].write_text(_manifest_body([str(record)]), encoding="utf-8")

    exit_code, findings = _check()

    assert exit_code == 0, findings
    assert findings == []


def test_the_refusal_names_the_record_and_the_missing_subject(
    review_run: dict[str, Path],
) -> None:
    """The refusal says what promotion would have said, against this record."""
    record = review_run["record"]
    record.write_text(json.dumps(_record({})), encoding="utf-8")
    review_run["manifest"].write_text(_manifest_body([str(record)]), encoding="utf-8")

    exit_code, findings = _check()

    assert exit_code != 0, findings
    assert all(
        finding.startswith(f"review record {str(record)!r} ") for finding in findings
    )
    assert any("has no subject" in finding for finding in findings), findings


def test_the_delivery_gate_itself_refuses_a_subjectless_record(
    review_run: dict[str, Path],
) -> None:
    """The gate that files the record, not only the pre-flight check, refuses it."""
    record = review_run["record"]
    record.write_text(json.dumps(_record({})), encoding="utf-8")
    review_run["manifest"].write_text(_manifest_body([str(record)]), encoding="utf-8")

    exit_code, output = _deliver()

    assert exit_code != 0, output
    assert "review_filed" in output, output
    assert "no subject" in output, output


def test_the_delivery_gate_passes_a_record_carrying_its_subject(
    review_run: dict[str, Path],
) -> None:
    """The same record naming its reviewed run clears the review-filing gate."""
    record = review_run["record"]
    record.write_text(
        json.dumps(_record({"reviewed_run_id": REVIEWED_RUN_ID})), encoding="utf-8"
    )
    review_run["manifest"].write_text(_manifest_body([str(record)]), encoding="utf-8")

    _exit_code, output = _deliver()

    # The gate ran to its promotion report, so the case proves the record cleared
    # the review-filing step rather than failing earlier on an unrelated guard.
    report = json.loads(output)
    assert "ok" in report, output
    assert "review_filed" not in report, output
    assert "no subject" not in output, output
