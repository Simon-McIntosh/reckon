"""The control is compared against the head arm, so a test-first node is admitted.

A node whose tests are written before the repair has every new case failing in
its baseline arm by design: the baseline is the suite run over the unfixed tree,
where the new cases are red because the feature they measure does not exist yet.
Comparing a control against that arm therefore refuses exactly the controls that
redden those cases — which is what the control is for — and the promotion can
only proceed on a waiver. The refusal is not a false negative the worker can
avoid: the node's own measurement is correct.

The admitting fact is therefore the one the control was written to establish:
the control's run exits non-zero and fails at least one test id the head arm
passes. The head arm is the node's own after measurement, so a case that is red
at the base and green at the head admits the control, and a case the head arm
still fails does not. The baseline comparison is kept as the fallback for a
manifest that records no readable head arm, which is the only case where it is
the best evidence available.

Cases (d) and (e) hold the two facts the admission has always required and must
keep: a log whose capture exited zero is refused, and a log with no exit record
is refused.

Completion is asked as a literal ``True``, never as a truthy stand-in, because
an arm that omits the key or carries null reads as complete while saying
nothing — the same mapping the strict arm validator refuses. So cases (f) to (h)
hold one manifest that differs only in that key: absent and null leave the
decision with the baseline comparison, which refuses the very same control that
a declared-complete head arm admits.

The same rule holds on both arms. A ``baseline_suite`` that omits the key or
carries null is unreadable too, so it is not the empty arm a manifest with no
baseline at all records: an unreadable baseline decides no control rather than
the absence of one. When a manifest records no readable arm of either kind the
comparison was never made, which admits nothing, so cases (i) and (j) hold one
unreadable baseline per form and assert the refusal — the control that a
declared-complete baseline would admit on the same log.

The ``failure_ids`` list is read the same way on both arms — a list of
non-empty strings — so cases (k) and (l) hold one baseline apart in that
field's shape alone: in list form it already fails the id the control
reddens and refuses the control, and any shape that cannot be read as ids —
a JSON string, a non-iterable, a null, a list carrying a non-string — is
unreadable and decides nothing rather than deciding as characters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon.crew.node import CrewError
from reckon.crew.promotion import _require_declared_negative_control

DECLARATION = "applied: the early return was removed in a scratch copy"
TEST_FIRST_CASE = "tests/test_guard.py::test_a_red_before_the_repair"
PASSED_CASE = "tests/test_guard.py::test_the_guard_reports"
HEAD_RED_CASE = "tests/test_guard.py::test_a_case_the_head_arm_also_fails"
KNOWN_CASE = "tests/test_guard.py::test_a_pre_existing_failure"
RUN_ID = "r-20260929T070000000000-control"


def _arm(
    failure_ids: list[str],
    *,
    revision: str,
    exit_status: int | None = None,
) -> dict:
    """One suite observation in the shape a manifest records it."""
    return {
        "revision": revision,
        "command": "pytest -p no:cacheprovider -q tests/",
        "exit_status": 1 if failure_ids else 0 if exit_status is None else exit_status,
        "log_path": f"logs/{revision}.log",
        "completed": True,
        "failure_count": len(failure_ids),
        "failure_ids": failure_ids,
    }


def _arm_without_completed_key(failure_ids: list[str], *, revision: str) -> dict:
    """One suite observation from a manifest that never declared completion."""
    observation = _arm(failure_ids, revision=revision)
    del observation["completed"]
    return observation


def _gate(
    tmp_path: Path,
    *,
    log_text: str,
    baseline: dict | None = None,
    after: dict | None = None,
    waiver: str = "",
) -> dict:
    """Run the gate over one red log and the manifest that names it."""
    log = tmp_path / "control.log"
    log.write_text(log_text, encoding="utf-8")
    manifest: dict = {"negative_control_log": log.name}
    if baseline is not None:
        manifest["baseline_suite"] = baseline
    if after is not None:
        manifest["after_suite"] = after
    record = {
        "run_id": RUN_ID,
        "node": {
            "id": "node-a",
            "write_paths": ["tests/test_guard.py"],
            "negative_control": DECLARATION,
        },
    }
    return _require_declared_negative_control(
        RUN_ID,
        record,
        gate="passed",
        manifest=manifest,
        manifest_path=str(tmp_path / "manifest.md"),
        waiver_reason=waiver,
    )


def _red_log(failure_id: str, *, exit_line: str = "EXIT=1\n") -> str:
    return (
        f"{DECLARATION}\n"
        f"FAILED {failure_id} - AssertionError: no refusal\n"
        "1 failed, 4 passed in 0.19s\n" + exit_line
    )


def test_a_case_red_at_the_base_and_green_at_the_head_admits_the_control(
    tmp_path: Path,
) -> None:
    """(a) test-first: the control reddens a case the head arm passes."""
    baseline = _arm([TEST_FIRST_CASE], revision="1111111")
    after = _arm([], revision="2222222")

    control = _gate(
        tmp_path, log_text=_red_log(TEST_FIRST_CASE), baseline=baseline, after=after
    )

    assert control["verdict"] == "matched"
    assert control["comparison_arm"] == "after_suite"
    assert control["head_failure_ids"] == []
    # The baseline id is still recorded beside the verdict, so a reader sees
    # why the two arms disagree on this case without reopening either log.
    assert control["baseline_failure_ids"] == [TEST_FIRST_CASE]
    assert control["added_failure_ids"] == [TEST_FIRST_CASE]


def test_a_case_the_head_arm_also_fails_is_refused(tmp_path: Path) -> None:
    """(b) A failure the head arm already has is not something the control broke."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(HEAD_RED_CASE),
            baseline=_arm([], revision="1111111"),
            after=_arm([HEAD_RED_CASE], revision="2222222"),
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert "head arm" in message
    assert HEAD_RED_CASE in message


@pytest.mark.parametrize(
    "after",
    [
        None,
        {**_arm([], revision="2222222"), "completed": False},
    ],
    ids=["absent", "uncompleted"],
)
def test_without_a_readable_head_arm_the_baseline_decides_the_refusal(
    tmp_path: Path, after: dict | None
) -> None:
    """(c1) No readable head arm, and the id the control failed is a baseline failure."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(KNOWN_CASE),
            baseline=_arm([KNOWN_CASE], revision="1111111"),
            after=after,
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert "baseline" in message
    assert KNOWN_CASE in message


@pytest.mark.parametrize(
    "after",
    [
        None,
        {**_arm([], revision="2222222"), "completed": False},
    ],
    ids=["absent", "uncompleted"],
)
def test_without_a_readable_head_arm_the_baseline_decides_the_admission(
    tmp_path: Path, after: dict | None
) -> None:
    """(c2) No readable head arm, and the id is one the baseline does not fail."""
    control = _gate(
        tmp_path,
        log_text=_red_log(PASSED_CASE),
        baseline=_arm([KNOWN_CASE], revision="1111111"),
        after=after,
    )

    assert control["verdict"] == "matched"
    assert control["comparison_arm"] == "baseline_suite"
    assert control["added_failure_ids"] == [PASSED_CASE]


def test_a_control_log_whose_run_exited_zero_is_refused(tmp_path: Path) -> None:
    """(d) The declaration and a failing id do not outrank the capture's own status."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(PASSED_CASE, exit_line="EXIT=0\n"),
            baseline=_arm([], revision="1111111"),
            after=_arm([], revision="2222222"),
        )

    message = str(refusal.value)
    assert "EXIT=0" in message


def test_a_control_log_with_no_exit_record_is_refused(tmp_path: Path) -> None:
    """(e) A fact nothing recorded is unknown, never inferred from the failing count."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(PASSED_CASE, exit_line=""),
            baseline=_arm([], revision="1111111"),
            after=_arm([], revision="2222222"),
        )

    message = str(refusal.value)
    assert "records no EXIT status" in message


def test_an_after_suite_without_a_completed_key_leaves_the_decision_to_the_baseline(
    tmp_path: Path,
) -> None:
    """(f) An arm that never declared completion is unreadable, so the baseline decides."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(TEST_FIRST_CASE),
            baseline=_arm([TEST_FIRST_CASE], revision="1111111"),
            after=_arm_without_completed_key([], revision="2222222"),
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert "baseline" in message
    assert TEST_FIRST_CASE in message


def test_an_after_suite_with_a_null_completed_value_leaves_the_decision_to_the_baseline(
    tmp_path: Path,
) -> None:
    """(g) Null declares no more completion than the key's absence does."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(TEST_FIRST_CASE),
            baseline=_arm([TEST_FIRST_CASE], revision="1111111"),
            after={**_arm([], revision="2222222"), "completed": None},
        )

    message = str(refusal.value)
    assert "adds no failure" in message
    assert "baseline" in message
    assert TEST_FIRST_CASE in message


def test_an_after_suite_declaring_completion_keeps_the_head_arm_decision(
    tmp_path: Path,
) -> None:
    """(h) The same manifest with completed true is decided by the head arm."""
    control = _gate(
        tmp_path,
        log_text=_red_log(TEST_FIRST_CASE),
        baseline=_arm([TEST_FIRST_CASE], revision="1111111"),
        after=_arm([], revision="2222222"),
    )

    assert control["verdict"] == "matched"
    assert control["comparison_arm"] == "after_suite"
    # The baseline fails the id the control reddens, which is why the arms that
    # decide matter: this is the test-first shape the two cases above refuse.
    assert control["baseline_failure_ids"] == [TEST_FIRST_CASE]
    assert control["added_failure_ids"] == [TEST_FIRST_CASE]


@pytest.mark.parametrize(
    "baseline",
    [
        _arm_without_completed_key([KNOWN_CASE], revision="1111111"),
        {**_arm([KNOWN_CASE], revision="1111111"), "completed": None},
    ],
    ids=["absent-key", "null"],
)
def test_a_baseline_that_declares_no_completion_decides_no_control(
    tmp_path: Path, baseline: dict
) -> None:
    """(i)/(j) An unreadable baseline is not an empty arm.

    The id the control reddens is one this baseline does not list, so read as
    complete it would admit the control. Read as unreadable it decides nothing,
    and with no head arm either there is no comparison left to admit it on.
    """
    with pytest.raises(CrewError) as refusal:
        _gate(tmp_path, log_text=_red_log(PASSED_CASE), baseline=baseline)

    message = str(refusal.value)
    assert "no readable comparison arm" in message
    assert PASSED_CASE in message


def test_a_declared_complete_baseline_admits_the_same_control(
    tmp_path: Path,
) -> None:
    """The contrast the two cases above turn on: the key alone changes the verdict."""
    control = _gate(
        tmp_path,
        log_text=_red_log(PASSED_CASE),
        baseline=_arm([KNOWN_CASE], revision="1111111"),
    )

    assert control["verdict"] == "matched"
    assert control["comparison_arm"] == "baseline_suite"
    assert control["added_failure_ids"] == [PASSED_CASE]


def test_a_baseline_that_lists_the_reddened_id_refuses_the_control(
    tmp_path: Path,
) -> None:
    """(k) In list form the baseline already fails the id the control reddens."""
    with pytest.raises(CrewError) as refusal:
        _gate(
            tmp_path,
            log_text=_red_log(PASSED_CASE),
            baseline=_arm([PASSED_CASE], revision="1111111"),
        )

    assert "adds no failure to the baseline" in str(refusal.value)


@pytest.mark.parametrize(
    "failure_ids",
    [
        PASSED_CASE,
        5,
        None,
        ["tests/test_guard.py::test_ok", 7],
    ],
    ids=["json-string", "non-iterable", "null", "non-string-element"],
)
def test_a_baseline_whose_failure_ids_is_not_a_list_of_ids_decides_no_control(
    tmp_path: Path, failure_ids: object
) -> None:
    """(l) An id set that cannot be read as one decides nothing, in every shape.

    The baseline lists the id the control reddens, so read as ids it refuses
    the control exactly as the list case above does. A value that cannot be
    read as a list of non-empty strings is unreadable instead: a JSON string
    would otherwise iterate as its characters and admit the control the list
    form refuses, and a non-iterable value would raise out of the gate. Either
    way no comparison is made, and with no head arm recorded it is refused.
    """
    baseline = {**_arm([PASSED_CASE], revision="1111111"), "failure_ids": failure_ids}

    with pytest.raises(CrewError) as refusal:
        _gate(tmp_path, log_text=_red_log(PASSED_CASE), baseline=baseline)

    assert "no readable comparison arm" in str(refusal.value)
