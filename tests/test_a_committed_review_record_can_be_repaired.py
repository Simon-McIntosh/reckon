"""A committed review record is repaired in place and the repair is committed.

A promotion lands each review record in the project's committed reviews tree,
``docs/state/<project>/reviews/run/<run>/<review-run>.json``. The repair verb
knew only the host review-records directory a live reviewer delivers into, so
a committed record had no repair path at all: the operator was told the record
was "outside the reviewed run's review-records directory" while the file sat
exactly where the promotion put it.

These tests write the record where a promotion writes it, drive the repair
through the command an operator runs, and read the result back through the
selection the promotion gate uses, so what is asserted is the record the gate
will see rather than the file the repair happened to rewrite.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import review as review_module
from reckon.crew.recovery_review_subject import select_review_for_head

PROJECT = "demo"
REVIEWED_RUN = "r-20261108T171139362143-review-subject"
REVIEW_RUN = "r-20261108T173448791464"
BASE = "a" * 40
HEAD = "b" * 40

# The status a repair moves a record to. ``superseded`` is derived rather than
# stored, so the stored vocabulary's own word is used.
REPAIRED_STATUS = "unparsed"


def _record() -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
        "status": "incomplete",
    }


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def committed_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project checkout whose committed run directory holds one record."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    repository = tmp_path / "checkout"
    docs = repository / "docs"
    run_directory = docs / "state" / PROJECT / "reviews" / "run" / REVIEWED_RUN
    run_directory.mkdir(parents=True)
    record = run_directory / f"{REVIEW_RUN}.json"
    record.write_text(json.dumps(_record(), indent=2) + "\n", encoding="utf-8")

    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "worker@example.invalid")
    _git(repository, "config", "user.name", "Worker")
    _git(repository, "add", "--", "docs")
    _git(repository, "commit", "-q", "-m", "chore: seed the committed reviews tree")

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(docs)}), encoding="utf-8"
    )
    return record


def _invoke(*args: str):
    return CliRunner().invoke(main, ["crew", *args])


def _repair(record: Path):
    return _invoke(
        "repair",
        "--review",
        "--run",
        REVIEWED_RUN,
        "--project",
        PROJECT,
        "--record",
        str(record),
        "--set",
        f"status={REPAIRED_STATUS}",
        "--reason",
        "the record stopped before it measured anything",
    )


def test_a_committed_record_is_repaired_and_the_repair_is_committed(
    committed_record: Path,
) -> None:
    repository = committed_record.parents[6]

    result = _repair(committed_record)

    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["reason"] == "the record stopped before it measured anything"

    relative = committed_record.relative_to(repository).as_posix()
    committed = _git(repository, "show", f"HEAD:{relative}")
    assert committed.returncode == 0, committed.stderr
    assert json.loads(committed.stdout)["status"] == REPAIRED_STATUS

    # The repair is its own commit, so the record is not left as a modified
    # tracked file the next reader of the checkout would trip on.
    assert _git(repository, "status", "--porcelain", "--", "docs").stdout.strip() == ""

    preserved = Path(receipt["as_delivered"])
    assert preserved.is_file()
    assert json.loads(preserved.read_text(encoding="utf-8")) == _record()

    # The selection the promotion gate reads returns the repaired record. The
    # as-delivered copy sits under a name the committed reader's ``*.json``
    # glob cannot select, or the original would win over the repair.
    assert not preserved.name.endswith(".json")
    assert preserved.name.startswith(committed_record.name)

    stored, stale = select_review_for_head(PROJECT, REVIEWED_RUN, HEAD)

    assert stale == ""
    assert stored is not None
    assert stored["status"] == REPAIRED_STATUS


def test_a_record_under_neither_location_is_still_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere" / "notes.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text(json.dumps(_record()), encoding="utf-8")

    result = _repair(elsewhere)

    assert result.exit_code == 1
    assert (
        "record is outside the reviewed run's review-records directory" in result.output
    )
    assert json.loads(elsewhere.read_text(encoding="utf-8")) == _record()
