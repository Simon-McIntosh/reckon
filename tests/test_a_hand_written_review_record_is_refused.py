"""check-manifest refuses a review record whose status no reader acts on.

A reviewer delivers its record by writing JSON straight into the record path
its dispatch granted — the one file a review produces, outside the repository.
``store_review`` checks the record's status before it writes one, because a
reader that selects ``parsed`` alone acts on nothing else; a reviewer that
composes the file by hand bypasses that check entirely, so a record carrying a
status no consumer reads sits on disk while the reviewed run reads as
unreviewed and promotion acts on that absence. Every reviewer passes
check-manifest before its turn ends, so the same status check is applied there,
through the one shared definition, and the refusal names the record, the status
found and the accepted set.

The refusal is shown to be the status check rather than a blanket one by
pairing it with a record written through ``store_review``, which passes the
same pass, and a placeholder that is not a review record is left alone — so the
check judges a record's status, not the mere existence of a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.review import (
    RECOGNISED_REVIEW_STATUSES,
    review_path,
    review_store_root,
    store_review,
)
from reckon.crew.runs import _write_json, crew_home, pointer_path

RUN_ID = "r-20260923T205045006945-gsag-review-record-status"
PROJECT = "nova"

# The record a reviewer writes by hand: a complete review body whose status no
# reader acts on, written straight into the granted record path.
HAND_WRITTEN_RECORD = {
    "project": PROJECT,
    "reviewed_run_id": RUN_ID,
    "status": "complete",
    "scores": {"goal_fidelity": 12},
    "findings": [],
}

# A record in the same shape written through the store: a recognised status,
# so the same check must accept it.
STORED_RECORD = {
    "project": PROJECT,
    "reviewed_run_id": RUN_ID,
    "status": "parsed",
    "scores": {"goal_fidelity": 12},
    "findings": [],
}

REVIEW_MANIFEST = """\
node: review-of-{run}
status: complete
commits: none
changed_paths:
{paths}
tests: record parsed, stored and parse-checked
"""


def _manifest_body(paths: list[str]) -> str:
    return REVIEW_MANIFEST.format(
        run=RUN_ID,
        paths="\n".join(f"  - {path}" for path in paths),
    )


def _check(run_id: str = RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


def _read(manifest: Path, body: str) -> tuple[int, list[str]]:
    """Write the body and return the exit code and findings the check reports."""
    manifest.write_text(body, encoding="utf-8")
    result = _check()
    return result.exit_code, json.loads(result.output)["findings"]


@pytest.fixture()
def review_run(isolated_reckon_home: Path, tmp_path: Path) -> dict[str, Path]:
    """A review run whose whole declared scope is the record it stores.

    Every directory this case resolves is named by the crew home the suite put
    in force, asserted here rather than assumed: a home resolution that stopped
    honouring the substitution would otherwise write a run pointer into the
    production tree this case has no business touching.
    """
    assert crew_home().is_relative_to(isolated_reckon_home)
    assert review_store_root().is_relative_to(isolated_reckon_home)

    record = review_path(PROJECT, RUN_ID)
    sibling = review_path(PROJECT, RUN_ID, reviewed_head_sha="a" * 40)
    record.parent.mkdir(parents=True, exist_ok=True)

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    manifest = tmp_path / "manifest.md"

    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": "0" * 40,
            "launch": "cli",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{RUN_ID}",
                "plan": "fixture",
                "section": "s6",
                "role": "review",
                "write_paths": [str(record), str(sibling)],
            },
        },
    )
    return {"record": record, "sibling": sibling, "manifest": manifest}


def test_a_hand_written_record_with_an_unrecognised_status_is_refused(
    review_run: dict[str, Path],
) -> None:
    """The bypass store_review closes is closed at check-manifest too."""
    record = review_run["record"]
    record.write_text(json.dumps(HAND_WRITTEN_RECORD), encoding="utf-8")
    exit_code, findings = _read(review_run["manifest"], _manifest_body([str(record)]))

    accepted = ", ".join(sorted(RECOGNISED_REVIEW_STATUSES))
    assert exit_code != 0, findings
    assert findings == [
        (
            f"review record {str(record)!r} review record carries status "
            f"'complete', which no reader acts on; the accepted statuses are "
            f"{accepted}"
        )
    ]


def test_a_record_written_through_the_store_passes(
    review_run: dict[str, Path],
) -> None:
    """The same check accepts the record the store itself would write."""
    written = store_review(dict(STORED_RECORD))

    assert written == review_run["record"]
    exit_code, findings = _read(review_run["manifest"], _manifest_body([str(written)]))
    assert exit_code == 0, findings
    assert findings == []


def test_a_placeholder_that_is_not_a_record_is_left_alone(
    review_run: dict[str, Path],
) -> None:
    """A file that names no subject and carries no material is not judged."""
    review_run["record"].write_text("{}\n", encoding="utf-8")
    exit_code, findings = _read(
        review_run["manifest"], _manifest_body([str(review_run["record"])])
    )

    assert exit_code == 0, findings
    assert findings == []


def test_a_non_review_run_writing_a_record_is_not_status_checked(
    review_run: dict[str, Path],
) -> None:
    """The check follows the review role, not the store path it happens to name."""
    record = review_run["record"]
    record.write_text(json.dumps(HAND_WRITTEN_RECORD), encoding="utf-8")
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(review_run["manifest"].parent / "repo"),
            "worktree": str(review_run["manifest"].parent / "repo"),
            "base_sha": "0" * 40,
            "launch": "cli",
            "role": "implement",
            "manifest_path": str(review_run["manifest"]),
            "node": {
                "id": "node-a",
                "plan": "fixture",
                "section": "s6",
                "role": "implement",
                "write_paths": [str(record)],
            },
        },
    )

    body = (
        "node: node-a\n"
        "status: complete\n"
        "commits: abc1234\n"
        "changed_paths:\n"
        f"  - {record}\n"
        "tests: focused check passed\n"
    )
    exit_code, findings = _read(review_run["manifest"], body)

    assert exit_code == 0, findings
    assert findings == []
