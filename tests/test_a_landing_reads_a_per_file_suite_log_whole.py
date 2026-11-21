"""A landing reads every file of a per-file suite log, not only the last.

A repository whose suite runs one process per test file appends every run into
one gate log, each run marked ``# file:`` and each closing with its own
``EXIT=`` record. The landing's receipt reader once took the log's last run
only, on the premise that several runs are successive attempts at one suite.
It therefore read the figure of a file that passed and refused an honest receipt
recording the six ids the gate failed, so a run whose base and head failed the
same ids was held as a red suite.

These cases drive the landing's own receipt checks over such a log: the receipt
agreement check the promotion runs, and the red-base admission that lets a
passing verdict stand beside that nonzero exit. Three marked files failing six
ids across the first and third read as one gate, so a head adding none reads as
zero added and the landing proceeds; a head adding one id in the middle file
reads one added and names it; and a log holding a single run is read exactly as
it was before the reader learned about marked files.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import promotion_evidence
from reckon.crew.node import CrewError

COMMAND = (
    "for f in tests/test_a.py tests/test_b.py tests/test_c.py; do "
    'PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q "$f"; done'
)

SIX_IDS = [f"tests/test_z.py::test_{name}" for name in "uvwxyz"]
ADDED_ID = "tests/test_b.py::test_regression"

BASE_FILES = [
    ("tests/test_a.py", SIX_IDS[:3]),
    ("tests/test_b.py", []),
    ("tests/test_c.py", SIX_IDS[3:]),
]

# The same six ids at head: the middle file still passes, so nothing was added.
SAME_HEAD_FILES = [
    ("tests/test_a.py", SIX_IDS[:3]),
    ("tests/test_b.py", []),
    ("tests/test_c.py", SIX_IDS[3:]),
]

# One added failure in the middle file, which passes on the base side.
ADDED_HEAD_FILES = [
    ("tests/test_a.py", SIX_IDS[:3]),
    ("tests/test_b.py", [ADDED_ID]),
    ("tests/test_c.py", SIX_IDS[3:]),
]

SINGLE_ID = "tests/test_z.py::test_u"
SINGLE_RUN_LOG = f"""\
FAILED {SINGLE_ID}
1 failed, 2 passed in 1.80s
EXIT=1
"""


def _per_file_log(files: list[tuple[str, list[str]]]) -> str:
    """One gate log holding a marked run per file, each closing in EXIT=."""
    lines = [f"# command: {COMMAND}"]
    for path, failed in files:
        lines.append(f"# file: {path}")
        lines.extend(f"FAILED {test_id}" for test_id in failed)
        lines.append(
            f"{len(failed)} failed, 2 passed in 0.20s"
            if failed
            else "3 passed in 0.10s"
        )
        lines.append(f"EXIT={1 if failed else 0}")
    lines.append("EXIT=1")
    return "\n".join(lines) + "\n"


def _arm(log: Path, ids: list[str]) -> dict:
    return {
        "revision": "a" * 40,
        "command": COMMAND,
        "exit_status": 1,
        "completed": True,
        "failure_count": len(ids),
        "failure_ids": list(ids),
        "log_path": str(log),
    }


def _landing(
    tmp_path: Path,
    base_log: str,
    base_ids: list[str],
    head_log: str,
    head_ids: list[str],
) -> tuple[dict, dict]:
    """The record and gate a landing holds for one base/head log pair."""
    base_path = tmp_path / "base.log"
    head_path = tmp_path / "head.log"
    base_path.write_text(base_log, encoding="utf-8")
    head_path.write_text(head_log, encoding="utf-8")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(
            [
                "status: complete",
                "baseline_suite: " + json.dumps(_arm(base_path, base_ids)),
                "after_suite: " + json.dumps(_arm(head_path, head_ids)),
                f"tests: {COMMAND}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record = {
        "manifest_path": str(manifest),
        "base_sha": "a" * 40,
        "repo": str(tmp_path),
        "worktree": str(tmp_path),
    }
    gate = {"log_path": str(head_path), "exit_status": 1, "command": COMMAND}
    return record, gate


def test_a_per_file_log_over_the_same_ids_reads_as_no_added_failure(
    tmp_path: Path,
) -> None:
    """Six ids failed in the first and third file read as the base's six."""
    record, gate = _landing(
        tmp_path,
        _per_file_log(BASE_FILES),
        SIX_IDS,
        _per_file_log(SAME_HEAD_FILES),
        SIX_IDS,
    )
    promotion_evidence._require_gate_log_agrees(
        "r-per-file-landing", gate, record, verdict="passed"
    )
    promotion_evidence._require_verdict_matches_exit_status(
        "r-per-file-landing", record, gate, verdict="passed"
    )


def test_a_head_adding_one_id_in_the_middle_file_is_named(tmp_path: Path) -> None:
    """The middle file's new failure is the one added id, and the landing says so."""
    record, gate = _landing(
        tmp_path,
        _per_file_log(BASE_FILES),
        SIX_IDS,
        _per_file_log(ADDED_HEAD_FILES),
        [*SIX_IDS, ADDED_ID],
    )
    promotion_evidence._require_gate_log_agrees(
        "r-per-file-landing", gate, record, verdict="passed"
    )
    with pytest.raises(CrewError) as refused:
        promotion_evidence._require_verdict_matches_exit_status(
            "r-per-file-landing", record, gate, verdict="passed"
        )
    assert ADDED_ID in str(refused.value)
    for covered in SIX_IDS:
        assert covered not in str(refused.value)


def test_a_single_run_log_reads_as_it_did_before(tmp_path: Path) -> None:
    """An unmarked log holds one run, described whole, and is judged as always."""
    record, gate = _landing(
        tmp_path, SINGLE_RUN_LOG, [SINGLE_ID], SINGLE_RUN_LOG, [SINGLE_ID]
    )
    assert (
        promotion_evidence._text_the_receipt_describes(SINGLE_RUN_LOG) == SINGLE_RUN_LOG
    )
    promotion_evidence._require_gate_log_agrees(
        "r-single-run-landing", gate, record, verdict="passed"
    )
    promotion_evidence._require_verdict_matches_exit_status(
        "r-single-run-landing", record, gate, verdict="passed"
    )
