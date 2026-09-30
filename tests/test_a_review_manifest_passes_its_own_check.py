"""A review's manifest passes the check its own delivery satisfies.

The check a worker runs against its manifest before it ends its turn is the same
audit promotion applies hours later, entered through the command surface. A
review's only deliverable is the record it stores beside the run it read, which
lies outside the repository, and it commits nothing — so a check that resolves
the store path to no repository root and calls it stray, or that asks a review
for a commit, refuses the manifest the store's own reviews write. Two reviews
were refused on exactly those findings.

Each case replays one shape from that field case through the command surface,
and each acceptance is paired with a run the same rule must still refuse, so the
acceptance is shown to be declaration-bound rather than blanket: the store path
passes because the dispatch granted it, and the commit finding is waived for the
role whose deliverable no commit records rather than for every role.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.review import review_path, review_store_root
from reckon.crew.runs import _write_json, crew_home, pointer_path

RUN_ID = "r-20260923T205045006945-gsag-production-solver-receipt-trip-alignment"
OTHER_RUN_ID = "r-20260923T205045006945-gsag-production-solver-receipt-reparse"
HEAD_SHA = "a3a7c58ce5792eed6d57adae6e54c48b08a86410"
PROJECT = "nova"

# The reviewer's own manifest, in the shape the two refused reviews wrote: the
# record it stored at the path its dispatch granted, marked complete, with no
# commit to cite because a review makes none.
REVIEW_MANIFEST = """\
node: review-of-{run}
status: complete
commits: none
changed_paths:
{paths}
tests: record parsed, stored and parse-checked
"""

COMMIT_FINDING = "status is complete but no commit is recorded"


def _record_and_sibling() -> tuple[Path, Path]:
    """The record the dispatch grants and the head-keyed copy beside it."""
    return (
        review_path(PROJECT, RUN_ID),
        review_path(PROJECT, RUN_ID, reviewed_head_sha=HEAD_SHA),
    )


def _manifest_body(paths: list[str]) -> str:
    return REVIEW_MANIFEST.format(
        run=RUN_ID,
        paths="\n".join(f"  - {path}" for path in paths),
    )


def _check(run_id: str = RUN_ID):
    return CliRunner().invoke(cli_main, ["crew", "check-manifest", "--run", run_id])


def _read(manifest: Path, body: str, run_id: str = RUN_ID) -> tuple[int, list[str]]:
    """Write the body and return the exit code and findings the check reports."""
    manifest.write_text(body, encoding="utf-8")
    result = _check(run_id)
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

    record, sibling = _record_and_sibling()
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("{}\n", encoding="utf-8")
    sibling.write_text("{}\n", encoding="utf-8")

    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(_manifest_body([str(record)]), encoding="utf-8")

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
                "section": "s4",
                "role": "review",
                "write_paths": [str(record)],
            },
        },
    )
    return {"record": record, "sibling": sibling, "manifest": manifest}


def test_a_review_naming_its_record_and_the_head_keyed_copy_passes(
    review_run: dict[str, Path],
) -> None:
    exit_code, findings = _read(
        review_run["manifest"],
        _manifest_body([str(review_run["record"]), str(review_run["sibling"])]),
    )

    assert exit_code == 0, findings
    assert findings == []


def test_a_review_naming_the_granted_record_alone_passes(
    review_run: dict[str, Path],
) -> None:
    exit_code, findings = _read(
        review_run["manifest"], _manifest_body([str(review_run["record"])])
    )

    assert exit_code == 0, findings
    assert findings == []


def test_a_review_naming_the_head_keyed_copy_alone_passes(
    review_run: dict[str, Path],
) -> None:
    """The copy beside the granted record is the same deliverable, keyed by head."""
    exit_code, findings = _read(
        review_run["manifest"], _manifest_body([str(review_run["sibling"])])
    )

    assert exit_code == 0, findings
    assert findings == []


def test_a_review_annotating_an_empty_changed_paths_list_passes(
    review_run: dict[str, Path],
) -> None:
    """A list annotated in place is that list; the prose is not a changed path."""
    exit_code, findings = _read(
        review_run["manifest"],
        "node: review-of-x\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []  (a review writes no repository path)\n"
        "tests: record parsed, stored and parse-checked\n",
    )

    assert exit_code == 0, findings
    assert findings == []


def test_a_review_naming_a_path_outside_its_declaration_is_still_refused(
    review_run: dict[str, Path],
) -> None:
    """The store path passes because it was granted, not because it leaves the repo."""
    undeclared = review_path(PROJECT, OTHER_RUN_ID)
    exit_code, findings = _read(
        review_run["manifest"], _manifest_body([str(undeclared)])
    )

    assert exit_code != 0
    assert findings == [f"changed paths outside the write scope: {undeclared}"]


def test_a_non_review_run_is_still_asked_for_the_commit_it_owes(
    review_run: dict[str, Path],
) -> None:
    """The waived finding follows the role, not the store path it wrote.

    The same manifest with the same declaration is refused for the commit when
    its role is one whose work a commit records, so the waiver rests on the
    review role rather than on the shape of the delivery.
    """
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
                "section": "s4",
                "role": "implement",
                "write_paths": [str(review_run["record"])],
            },
        },
    )

    exit_code, findings = _read(
        review_run["manifest"], _manifest_body([str(review_run["record"])])
    )

    assert exit_code != 0
    assert findings == [COMMIT_FINDING]


def test_a_run_inside_its_repository_declaration_still_passes(tmp_path: Path) -> None:
    """The positive control: the ordinary in-repository shape is unchanged."""
    repository = tmp_path / "repo"
    repository.mkdir()
    manifest = tmp_path / "manifest.md"
    pointer_path_value = "candidate.txt"
    _write_json(
        pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": "0" * 40,
            "launch": "cli",
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": "node-a",
                "plan": "fixture",
                "section": "s4",
                "role": "implement",
                "write_paths": [pointer_path_value],
            },
        },
    )

    exit_code, findings = _read(
        manifest,
        "node: node-a\n"
        "status: complete\n"
        "commits: abc1234\n"
        f"changed_paths: {pointer_path_value}\n"
        "tests: focused check passed\n",
    )

    assert exit_code == 0, findings
    assert findings == []
