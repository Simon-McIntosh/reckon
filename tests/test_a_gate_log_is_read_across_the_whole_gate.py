"""A gate log's failure figures are read across every run the gate produced.

A merge refused a passing gate because its receipt recorded the 32 failures a
multi-label ctest gate had inherited, while the log's *last* label ran clean.
The agreement check read only the log's last run, so it compared an honest
whole-gate receipt against a clean tail and reported a contradiction.

Each case below is one shape the agreement check meets over a temporary log and
manifest: a two-label ctest gate whose 32 first-label failures are recorded by
the arm and accepted, a failure counted from the second label as well as the
whole gate's total, a receipt one short of the whole gate's count refused, a
receipt naming a test the gate never reported refused, and a pytest log still
read from its own last run so a receipt copied from the earlier run is refused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion_evidence import _require_gate_log_agrees

MULTI_LABEL_COMMAND = (
    "ctest --output-on-failure -L VALIDATION; ctest --output-on-failure -L QUICK"
)
PYTEST_COMMAND = (
    "PYTHONPATH=. .venv/bin/python -m pytest -p no:cacheprovider -q tests/test_x.py"
)

_INHERITED_FAILURES = [
    "validate/ITER/imas/nice-58072",
    "validate/ITER/imas/nice-63159",
    "validate/ITER/imas/nice-62751",
    "validate/WEST/imas/nice-58072",
    "validate/WEST/imas/nice-63159",
    "validate/WEST/imas/nice-62751",
    "validate/WEST/imas/nice-58072__dd3.37.0",
    "validate/WEST/imas/nice-63159__dd3.42.0",
    "validate/WEST/imas/nice-62751__dd3.42.0",
    "validate/WEST/imas/nice-62751-internal",
    "validate/ITER/imas/dina3420",
    "validate/TCV/imas/magnetics-77549/liuqe",
    "validate/TCV/imas/magnetics-77549/dina",
    "validate-quick/TCV/imas/magnetics-77549/liuqe",
    "validate-quick/MASTU/imas/magnetics-v4-1-0",
    "validate-quick/HL-3/imas/magnetics",
    "validate/ITER/imas/corsica",
    "validate/ITER/imas/astra",
    "validate/ITER/imas/dina105027",
    "validate-quick/ITER/imas/fullPulse",
    "validate/ITER/imas/solps",
    "validate/ITER/imas/jorek",
    "validate/ITER/imas/fastran",
    "validate/SPARC/imas/eq",
    "validate/EAST/imas/magnetics",
    "validate/ITER/imas/create",
    "validate/ITER/imas/step",
    "validate/JET/imas/eq",
    "validate/KSTAR/imas/eq",
    "validate/EAST/imas/liuqe",
    "validate/EAST/imas/toki",
    "validate/HL-3/imas/nice-58072",
]
_WHOLE_GATE_COUNT = len(_INHERITED_FAILURES)


def _ctest_run(names: list[str], total: int, exit_status: int) -> str:
    """One ctest label's run, as ctest writes it, ending in its own capture."""
    failed = len(names)
    body = "".join(
        f"\t{index} - {name} (Failed)\n"
        for index, name in enumerate(names, start=1)
    )
    return (
        f"Test project /build\n"
        f"      Start 1: {names[0] if names else 'quick/one'}\n"
        f"1/{total} Test #1: {names[0] if names else 'quick/one'} ...   "
        f"{'***Failed' if names else 'Passed'}    0.31 sec\n"
        f"\nThe following tests FAILED:\n{body}Errors while running CTest\n"
        f"{100 - failed * 100 // total}% tests passed, {failed} tests failed "
        f"out of {total}\nEXIT={exit_status}\n"
    )


# The gate imas-efit reported: the validation label inherits 32 failures and the
# quick label that follows it runs clean, so the log's tail holds no failure at
# all while the gate as a whole holds 32.
TWO_LABEL_LOG = _ctest_run(_INHERITED_FAILURES, 59, 8) + _ctest_run([], 5, 0)

# A failure in each label: the gate's total is both runs' failures together, and
# a reader that stopped at the first label's block would report only two of them.
_SPLIT_FIRST = ["validate/A/two"]
_SPLIT_SECOND = ["validate/B/one"]
SPLIT_LABEL_LOG = _ctest_run(_SPLIT_FIRST, 10, 8) + _ctest_run(_SPLIT_SECOND, 4, 0)

# Two pytest runs captured into one log: the tail's status belongs to the second
# run, so a receipt carrying the first run's figures describes a run the status
# line beside it does not belong to.
TWO_PYTEST_RUNS = """\
FAILED tests/test_x.py::test_one
FAILED tests/test_x.py::test_two
1 passed, 2 failed in 0.20s
EXIT=1
3 passed in 0.10s
EXIT=0
"""


def _guard(tmp_path: Path, log_text: str, arm: dict, *, command: str) -> dict:
    """Run the agreement check over a temporary log and manifest.

    The manifest carries only what the check reads: a recognised status and the
    ``after_suite`` arm naming this log.
    """
    log = tmp_path / "gate.log"
    log.write_text(log_text, encoding="utf-8")
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        "\n".join(
            ["status: complete", f"after_suite: {json.dumps(arm)}", f"tests: {command}"]
        )
        + "\n",
        encoding="utf-8",
    )
    record = {"manifest_path": str(manifest)}
    gate = {"log_path": str(log), "exit_status": 0, "command": command}
    _require_gate_log_agrees("r-multi-label", gate, record, verdict="passed")
    return record


def _arm(tmp_path: Path, *, count: int, ids: list[str]) -> dict:
    return {
        "log_path": str(tmp_path / "gate.log"),
        "exit_status": 0,
        "completed": True,
        "failure_count": count,
        "failure_ids": ids,
    }


def test_a_multi_label_gate_is_compared_whole(tmp_path: Path) -> None:
    """The 32 inherited failures are the gate's figure, though its tail is clean."""
    _guard(
        tmp_path,
        TWO_LABEL_LOG,
        _arm(tmp_path, count=_WHOLE_GATE_COUNT, ids=_INHERITED_FAILURES),
        command=MULTI_LABEL_COMMAND,
    )


def test_a_failure_in_a_later_label_is_counted(tmp_path: Path) -> None:
    """Both labels' failures belong to the gate, not only the first block's."""
    _guard(
        tmp_path,
        SPLIT_LABEL_LOG,
        _arm(tmp_path, count=2, ids=[*_SPLIT_FIRST, *_SPLIT_SECOND]),
        command=MULTI_LABEL_COMMAND,
    )


def test_a_multi_label_receipt_one_short_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CrewError) as refused:
        _guard(
            tmp_path,
            TWO_LABEL_LOG,
            _arm(tmp_path, count=_WHOLE_GATE_COUNT - 1, ids=_INHERITED_FAILURES),
            command=MULTI_LABEL_COMMAND,
        )
    assert f"failure_count records {_WHOLE_GATE_COUNT - 1}" in str(refused.value)
    assert f"names {_WHOLE_GATE_COUNT} failed" in str(refused.value)


def test_a_multi_label_receipt_naming_an_unknown_id_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(CrewError) as refused:
        _guard(
            tmp_path,
            TWO_LABEL_LOG,
            _arm(
                tmp_path,
                count=_WHOLE_GATE_COUNT,
                ids=[*_INHERITED_FAILURES, "validate/GHOST/impossible"],
            ),
            command=MULTI_LABEL_COMMAND,
        )
    assert "validate/GHOST/impossible" in str(refused.value)


def test_a_pytest_log_is_still_read_from_its_last_run(tmp_path: Path) -> None:
    with pytest.raises(CrewError) as refused:
        _guard(
            tmp_path,
            TWO_PYTEST_RUNS,
            _arm(tmp_path, count=2, ids=["tests/test_x.py::test_one", "tests/test_x.py::test_two"]),
            command=PYTEST_COMMAND,
        )
    assert "failure_count records 2" in str(refused.value)