"""A dispose refusal names a review run whose record is filed but not promoted.

A run review writes its record inside the directory of the run it reviews and a
promotion files that copy into the project's review store. A disposition reads
the store, so between those two moments the record exists and the store holds
none: a refusal that says only that no record was found describes a lookup
defect that is not there and leaves the operator searching for one.

The refusal must name the review run as not yet promoted and carry the command
that promotes it. Once that promotion has filed the record, the same
disposition must succeed, and a review run that produced no record at all must
keep the project's existing refusal.

Every crew directory resolves inside a temporary configuration home, so nothing
touches the operator's own stores.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import recovery_review_delivery as delivery
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "dispose-unpromoted-fixture"
SESSION = "coordinator-fixture"
REVIEWED_RUN = "r-20261120T120000000000-reviewed-run"
REVIEW_RUN = "r-20261120T130000000000-figure-review-run"
ABSENT_REVIEW_RUN = "r-20261120T140000000000-no-such-review"
FOLD_NODE = "fix-a"

FINDINGS: list[dict[str, Any]] = [
    {
        "file": "docs/figures/p/p.svg",
        "line": "1",
        "text": "the axis label is clipped at the canvas edge",
        "severity": "blocking",
    },
]


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its report from."""

    config_home: Path
    repository: Path
    head_sha: str
    record_path: Path
    review_pointer: dict[str, Any]


def _seed_checkout(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A git checkout under the temp tree, and its one commit's revision."""
    repository = tmp_path / name
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fixture checkout"),
    ):
        _git(repository, *arguments)
    return repository, _git(repository, "rev-parse", "HEAD")


def _write_pointer(
    run_id: str,
    *,
    worktree: Path,
    node_id: str | None = None,
    write_paths: list[str] | None = None,
) -> dict[str, Any]:
    pointer = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "process_alive": False,
        "repo": str(worktree),
        "worktree": str(worktree),
        "node": {
            "id": node_id or run_id,
            "plan": "",
            "section": "",
            "write_paths": write_paths or ["docs"],
        },
    }
    runs._write_json(runs.pointer_path(run_id), pointer)
    return pointer


def _mount(config_home: Path, repository: Path) -> None:
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A live review run whose record is filed in the reviewed run's directory."""
    repository, head_sha = _seed_checkout(tmp_path, "repo")
    _mount(isolated_reckon_home, repository)
    _write_pointer(REVIEWED_RUN, worktree=repository)
    record_path = review_module.run_review_record_paths(
        runs.run_dir(REVIEWED_RUN), REVIEWED_RUN
    )[0]
    record_path = Path(record_path)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "review_run_id": REVIEW_RUN,
                "status": "parsed",
                "reviewed_base_sha": head_sha,
                "reviewed_head_sha": head_sha,
                "findings": FINDINGS,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    review_pointer = _write_pointer(
        REVIEW_RUN,
        worktree=repository,
        node_id=f"review-of-{REVIEWED_RUN}",
        write_paths=[str(record_path)],
    )
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        head_sha=head_sha,
        record_path=record_path,
        review_pointer=review_pointer,
    )


def _dispose(*arguments: str):
    """Invoke the disposition command against the fixture project and run."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            REVIEWED_RUN,
            *arguments,
        ],
    )


def test_the_refusal_names_the_unpromoted_review_run(fixture: Fixture) -> None:
    """A filed record the store has not received names the unpromoted review."""
    assert review_module.stored_record(PROJECT, REVIEWED_RUN)[1] is None
    filed_before = fixture.record_path.read_text(encoding="utf-8")

    result = _dispose(
        "--review",
        REVIEW_RUN,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )

    assert result.exit_code != 0, result.output
    # The refusal names the state, not a missing record, and names the review
    # run it is about.
    assert "not yet promoted" in result.output
    assert REVIEW_RUN in result.output
    # ... and carries the command that lands the record on the store.
    assert f"reckon crew complete --run {REVIEW_RUN}" in result.output
    # It is not the refusal a review that produced nothing gets.
    assert "carries no record from review run" not in result.output
    # Nothing was written anywhere: the store still holds no record and the
    # filed copy is byte-identical.
    assert review_module.stored_record(PROJECT, REVIEWED_RUN)[1] is None
    assert fixture.record_path.read_text(encoding="utf-8") == filed_before


def test_the_disposal_succeeds_once_the_review_is_promoted(fixture: Fixture) -> None:
    """Filing the record, as a promotion does, lets the disposition answer it."""
    preferred = _dispose(
        "--review",
        REVIEW_RUN,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert preferred.exit_code != 0, preferred.output
    assert "not yet promoted" in preferred.output

    # A promotion's own step is what files a delivered record into the store.
    filed = delivery.file_review_records_into_store(fixture.review_pointer)
    assert filed, "the delivered record was not filed into the store"
    assert review_module.stored_record(PROJECT, REVIEWED_RUN)[1] is not None

    result = _dispose(
        "--review",
        REVIEW_RUN,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 0
    assert receipt["disposition"] == f"folded:{FOLD_NODE}"


def test_a_review_run_with_no_record_anywhere_keeps_the_existing_refusal(
    fixture: Fixture,
) -> None:
    """A review run that wrote no record at all is refused as it is today."""
    result = _dispose(
        "--review",
        ABSENT_REVIEW_RUN,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert result.exit_code != 0, result.output
    assert "carries no record from review run" in result.output
    assert "not yet promoted" not in result.output
