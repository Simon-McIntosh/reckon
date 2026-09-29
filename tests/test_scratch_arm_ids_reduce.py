"""A base arm run inside a scratch tree compares its ids in repository-relative form.

pytest prints a summary line's node id relative to the root directory it
resolved, and that directory can sit above the arm's own working directory: an
arm run as ``env -C /tmp/rcq2-base`` whose root resolved to ``/tmp`` printed the
ids as ``rcq2-base/tests/x.py::t`` where the head arm at the repository root
printed ``tests/x.py::t``. Compared verbatim one test reads as two, a run that
added nothing reports added failures, and the nonzero count caps its total below
the promotion floor — the direction the count exists to prevent.

These tests drive the production read path (``annotate_review_of_run``) with a
synthesised manifest and the two arm logs it names, and derive the base log's
spelling from the directory the arm's own record names rather than writing the
spelling down. That record marks the directory in the two shapes a real manifest
uses: the ``env -C`` target its gate command pins the run to, and the manifest's
own ``measurement_cwd``. A path whose leading components are part of the
repository-relative path — a package's own ``pkg/tests/`` — shares no suffix
with the record, is not the scratch tree's contribution, and stays a different
test: merging the two would lose a failure the head really added.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import review as review_module
from reckon.crew.runs import run_dir

PROJECT = "proj"
RUN = "r-20260929T000000000000-scratch-arm"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

# The scratch tree a base arm ran inside. Nothing resolves it on disk: the
# reduction reads the recorded path, and the test never touches the tree.
SCRATCH = "/tmp/rcq2-base"  # noqa: S108 — fixture path a scratch arm printed, never opened

REPO_IDS = ["tests/test_alpha.py::test_alpha", "tests/test_beta.py::test_beta"]
PACKAGE_ID = "pkg/tests/test_gate.py::test_alpha"
REPO_TESTS_ID = "tests/test_gate.py::test_alpha"

SCORE = 18
TOTAL = SCORE * len(review_module.REVIEW_DIMENSIONS)

BASE_COMMAND = f"env -C {SCRATCH} python -m pytest tests/test_alpha.py"
HEAD_COMMAND = "PYTHONPATH=<worktree> python -m pytest tests/test_alpha.py"


def _prefixed(test_id: str, directory: str) -> str:
    """Return the spelling a pytest log printed for ``test_id`` from ``directory``.

    The prefix is read off the recorded directory rather than written down, so a
    reduction that ignores the record cannot pass by matching a literal.
    """
    return f"{Path(directory).name}/{test_id}"


def _log(*failed: str) -> str:
    lines = [f"FAILED {test_id} - AssertionError: boom" for test_id in failed]
    lines.append(f"{len(failed)} failed")
    return "\n".join(lines) + "\n"


def _suite(log_path: str, command: str) -> str:
    return json.dumps(
        {
            "revision": HEAD_SHA,
            "command": command,
            "exit_status": 1,
            "log_path": log_path,
            "failure_ids": [],
            "completed": True,
        }
    )


def _annotated_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    base_ids: list[str],
    head_ids: list[str],
    base_command: str = BASE_COMMAND,
    extra: str = "",
) -> dict:
    """Write the two arms and read them back through the promotion's own path."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    directory = run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "base.log").write_text(_log(*base_ids), encoding="utf-8")
    (directory / "head.log").write_text(_log(*head_ids), encoding="utf-8")
    (directory / "manifest.md").write_text(
        f"node: {RUN}\n"
        "status: complete\n"
        f"{extra}"
        f"baseline_suite: {_suite('base.log', base_command)}\n"
        f"after_suite: {_suite('head.log', HEAD_COMMAND)}\n",
        encoding="utf-8",
    )

    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, SCORE),
        "absent": [],
        "total": TOTAL,
        review_module.REVIEWED_BASE_KEY: BASE_SHA,
        review_module.REVIEWED_HEAD_KEY: HEAD_SHA,
        "timestamp": "2026-09-29T00:00:00+00:00",
    }
    return review_module.annotate_review_of_run(record, RUN)


@pytest.mark.parametrize("recorded_as", ["env -C", "measurement_cwd"])
def test_a_scratch_tree_prefix_the_base_record_names_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_as: str
) -> None:
    """The base arm's own recorded directory explains the prefix its log printed.

    Two failures are present in both arms and only the base log's spelling
    differs, by exactly the prefix that arm's record accounts for. Read against
    each other the spellings report two added failures and cap the total below
    the promotion floor.
    """
    base_command = BASE_COMMAND
    extra = ""
    if recorded_as == "measurement_cwd":
        base_command = (
            f"PYTHONPATH={SCRATCH} python -m pytest {SCRATCH}/tests/test_alpha.py"
        )
        extra = f"measurement_cwd: {SCRATCH}\n"

    review = _annotated_record(
        tmp_path,
        monkeypatch,
        base_ids=[_prefixed(test_id, SCRATCH) for test_id in REPO_IDS],
        head_ids=list(REPO_IDS),
        base_command=base_command,
        extra=extra,
    )

    assert review["added_failure_count"] == 0, (
        "both arms report the same two tests; the base log's prefix is the tree "
        "its own record names, so nothing was added and the total must not be "
        "capped"
    )
    assert review["added_failure_ids"] == []
    assert review["total"] == TOTAL


def test_a_package_tests_id_stays_distinct_from_the_repository_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scratch prefix reduces away; a package's own ``tests`` directory does not.

    The base arm's ``rcq2-base/pkg/tests/…`` and the head arm's ``tests/…`` name
    two different tests, and the head's is the one the head alone reports.
    Reducing both onto the repository anchor reports that added failure as none,
    the direction opposite the phantom this reduction exists to prevent.
    """
    review = _annotated_record(
        tmp_path,
        monkeypatch,
        base_ids=[_prefixed(PACKAGE_ID, SCRATCH)],
        head_ids=[REPO_TESTS_ID],
        base_command=f"env -C {SCRATCH} python -m pytest {PACKAGE_ID}",
    )

    assert review["added_failure_count"] == 1, (
        "the head's tests/test_gate.py::test_alpha is not the pkg/tests/… test "
        "the base log reports; merging them loses a real added failure"
    )
    assert review["added_failure_ids"] == [REPO_TESTS_ID]
