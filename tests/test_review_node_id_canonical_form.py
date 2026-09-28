"""The added-failure count compares node ids in one repository-relative form.

A gate log carries whatever spelling the command that wrote it printed. A head
arm invoked from a different working directory reports its summary lines with
the path relative to that directory (``../../home/…/tests/x.py``) or absolute,
where the base arm run at the repository root reported ``tests/x.py``. Compared
verbatim the same test reads as one the run added, and a nonzero count caps a
sound run's total below the promotion floor — the direction the count exists to
prevent.

These tests drive ``added_failures_from_gate_logs`` with synthesised logs
carrying the two spellings of one node id, and require the count to be a
difference over the ids themselves. The ``::`` segments and parametrisation
brackets are left intact, because they are what distinguishes two tests sharing
a file, and an id carrying no repository-relative marker is kept verbatim and
still compared rather than dropped from the difference.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery as recovery_module
from reckon.crew import review as review_module
from reckon.crew.runs import run_dir

PROJECT = "proj"
RUN = "r-20260927T000000000000-reviewed-run"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

RELATIVE = "tests/test_gate.py::test_alpha"
ABSOLUTE = (
    "/home/ITER/mcintos/Code/.reckon-worktrees/reckon-abc123/added"
    "/tests/test_gate.py::test_alpha"
)
DOTDOT = (
    "../../home/ITER/mcintos/Code/.reckon-worktrees/reckon-abc123/added"
    "/tests/test_gate.py::test_alpha"
)
# A path with no ``tests`` component carries nothing the repository can be
# resolved against, whatever directory it names.
UNRESOLVABLE = "/opt/other/suite/check.py::test_z"
# A second, distinct test whose path differs from RELATIVE only above a
# component that is itself named ``tests``: a package's own test directory, not
# a working-directory prefix, so the two must not reduce onto one id.
NESTED = "pkg/tests/test_gate.py::test_alpha"

SCORE = 18
TOTAL = SCORE * len(review_module.REVIEW_DIMENSIONS)


def _log(*failed: str) -> str:
    lines = [f"FAILED {test_id} - AssertionError: boom" for test_id in failed]
    lines.append(f"{len(failed)} failed")
    return "\n".join(lines) + "\n"


def _count(
    base_failed: list[str], head_failed: list[str]
) -> tuple[int | None, list[str]]:
    return review_module.added_failures_from_gate_logs(
        _log(*base_failed), _log(*head_failed)
    )


@pytest.mark.parametrize("head_spelling", [ABSOLUTE, DOTDOT])
def test_the_same_test_under_two_id_forms_is_not_added(head_spelling: str) -> None:
    """One test, green at both revisions, must not read as a failing addition."""
    count, added = _count([RELATIVE], [head_spelling])

    assert count == 0, (
        "the head log names the same test as the base log under a path written "
        f"from another working directory ({head_spelling!r}); counting it as "
        "added caps a sound run's total"
    )
    assert added == []


@pytest.mark.parametrize("head_spelling", [ABSOLUTE, DOTDOT])
def test_a_genuinely_new_id_is_added_under_either_form(head_spelling: str) -> None:
    """Canonicalising must not swallow a test the head arm really added."""
    new_id = "tests/test_new.py::test_added"
    new_spelling = head_spelling.replace(
        "test_gate.py::test_alpha", "test_new.py::test_added"
    )
    assert new_spelling != head_spelling

    count, added = _count([RELATIVE], [new_spelling])

    assert count == 1, "a failing test the base never ran is an added failure"
    assert added == [new_id], "the added id is reported in repository-relative form"


def test_two_tests_sharing_a_file_stay_distinct() -> None:
    """The file canonicalises; the ``::`` segments must keep the two apart."""
    count, added = _count(
        ["tests/test_gate.py::test_alpha"],
        ["/w/worktrees/reckon-abc123/added/tests/test_gate.py::test_beta"],
    )

    assert count == 1, (
        "test_beta fails at the head and never ran at the base; a rule that "
        "reduced ids to a file would lose that"
    )
    assert added == ["tests/test_gate.py::test_beta"]


def test_path_separators_are_normalised() -> None:
    """A log written where the separator is a backslash names the same test."""
    count, added = _count(
        ["tests/test_gate.py::test_alpha"], ["tests\\test_gate.py::test_alpha"]
    )

    assert count == 0
    assert added == []


def test_class_and_parametrisation_segments_survive() -> None:
    """Brackets and class segments are kept, and still tell cases apart."""
    same = "tests/test_gate.py::TestShaper::test_case[1-2]"
    other = "tests/test_gate.py::TestShaper::test_case[3-4]"

    count, added = _count(
        [same], ["/w/added/tests/test_gate.py::TestShaper::test_case[1-2]"]
    )
    assert count == 0, "a parametrised id must canonicalise with its bracket intact"
    assert added == []

    count, added = _count(
        [same], ["/w/added/tests/test_gate.py::TestShaper::test_case[3-4]"]
    )
    assert count == 1, "a different parametrisation of the same test is a different id"
    assert added == [other]


def test_an_id_carrying_no_repository_marker_is_kept_verbatim() -> None:
    """Unresolvable is compared as spelled, never dropped from the difference."""
    count, added = _count([UNRESOLVABLE], [UNRESOLVABLE])
    assert count == 0, "an id spelled the same on both sides is not an addition"
    assert added == []

    count, added = _count(["tests/test_gate.py::test_alpha"], [UNRESOLVABLE])
    assert count == 1, "an unresolvable id that the head alone reports is still added"
    assert added == [UNRESOLVABLE], "it is reported under the spelling its log used"


def test_ids_differing_above_a_tests_component_stay_distinct() -> None:
    """A nested ``tests`` directory is a different test, not a shifted prefix.

    The reduction exists to stop a prefix the working directory contributed
    from telling one test from another. A component above the anchor that is
    part of the repository-relative path is not that prefix. Reducing on it
    merges two tests, and an added failure under one is then not counted
    against the base log that reports the other — a silent under-count, which
    is the direction opposite to the phantom this count was repaired against.
    """
    count, added = _count([NESTED], [RELATIVE])

    assert count == 1, (
        "the head's tests/test_gate.py::test_alpha is not the pkg/tests/… test "
        "the base log reports; merging them reports an added failure as none"
    )
    assert added == [RELATIVE]

    count, added = _count([RELATIVE], [NESTED])

    assert count == 1, "the reverse direction under-counts by the same merge"
    assert added == [NESTED]


def test_a_working_directory_prefix_reduces_but_a_nested_one_does_not() -> None:
    """Both halves of the rule, side by side, so a change to either is visible.

    One id printed two ways is still one id; two ids differing only above a
    ``tests`` component are still two. A reduction that satisfies only one half
    moves the count silently in one direction or the other.
    """
    assert review_module.canonical_node_id(ABSOLUTE) == RELATIVE
    assert review_module.canonical_node_id(DOTDOT) == RELATIVE
    assert review_module.canonical_node_id(RELATIVE) == RELATIVE
    assert review_module.canonical_node_id(NESTED) == NESTED


def test_a_path_opening_with_the_repository_directory_name_reduces() -> None:
    """An arm run from the repository's parent names the repository directory first.

    The component records where the command ran, not part of the path: the base
    log's ``tests/test_gate.py::test_alpha`` is the same test, and comparing the
    two spellings verbatim reports it added. A first component that is part of
    the repository-relative path — a package's own ``pkg/tests/…`` — is not
    this case and stays distinct, which the assertion on NESTED below pins. The
    name is read from the module rather than written down because a worktree
    sits under its node's name; in the main checkout it is ``reckon``, so the
    spelling there is ``reckon/tests/test_gate.py::test_alpha``.
    """
    spelled = f"{review_module._REPO_DIRECTORY_NAME}/{RELATIVE}"
    nested_spelled = f"{review_module._REPO_DIRECTORY_NAME}/{NESTED}"

    assert review_module.canonical_node_id(spelled) == RELATIVE
    assert review_module.canonical_node_id(nested_spelled) == NESTED

    count, added = _count([RELATIVE], [spelled])

    assert count == 0, (
        "both logs name the same test; the head arm's own directory prefix is "
        "where it ran, not a different test, and counting it caps the total"
    )
    assert added == []


def test_retirement_prose_spelling_a_shifted_prefix_retires_the_canonical_id() -> None:
    """A manifest names an added id as the manifest knows it, and still retires it.

    The id the count derives comes from the gate log, which a head arm invoked
    from another working directory prints with that directory's prefix; the
    manifest retires it by naming the test. Compared as spellings the two
    disagree and a retired failure caps the total below the promotion floor.
    """
    added_id = "tests/test_added.py::test_added"
    shifted = ABSOLUTE.replace("test_gate.py::test_alpha", "test_added.py::test_added")
    assert shifted != ABSOLUTE

    annotated = review_module.annotate_added_failures(
        {"total": TOTAL},
        base_text=_log(),
        head_text=_log(shifted),
        retirement_text=f"the suite is green apart from {shifted}, retired by name",
    )

    assert annotated["added_failure_count"] == 1
    assert annotated["added_failure_ids"] == [added_id], (
        "the count reports the id in canonical form, whatever the log spelled"
    )
    assert annotated["total"] == TOTAL, (
        "the manifest retired the added id by name, so the total must not be "
        "capped below the added-failures cap"
    )
    assert "not capped" in annotated["added_failures_note"]


def test_a_comma_joined_retirement_sentence_retires_the_ids_it_names() -> None:
    """A retirement sentence grouping ids as a manifest list field does retires each.

    Manifests write id lists comma separated and retire them the same way, so a
    sentence naming two ids as ``a,b`` retires both. Splitting the sentence on
    whitespace alone leaves the pair as one token that names no id at all, and
    both then count as unretired additions, capping the total below the
    promotion floor.
    """
    first = "tests/test_added.py::test_added"
    second = "tests/test_other.py::test_other"

    annotated = review_module.annotate_added_failures(
        {"total": TOTAL},
        base_text=_log(),
        head_text=_log(first, second),
        retirement_text=(
            f"the suite is green apart from {first},{second}, retired by name"
        ),
    )

    assert annotated["added_failure_count"] == 2
    assert annotated["added_failure_ids"] == [first, second], (
        "each id is counted, in the canonical form the logs reduce to"
    )
    assert annotated["total"] == TOTAL, (
        "the sentence retired both ids by name, so neither is an unretired "
        "addition and the total must not be capped"
    )
    assert "not capped" in annotated["added_failures_note"]


def test_a_head_log_written_from_another_cwd_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read path a promotion uses returns no addition for the shifted id."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))

    directory = run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "base.log").write_text(_log(RELATIVE), encoding="utf-8")
    (directory / "head.log").write_text(_log(ABSOLUTE), encoding="utf-8")
    (directory / "manifest.md").write_text(
        "node: "
        + RUN
        + "\nstatus: complete\n"
        + "baseline_suite: "
        + json.dumps({"log_path": "base.log", "failure_ids": [RELATIVE]})
        + "\n"
        + "after_suite: "
        + json.dumps({"log_path": "head.log", "failure_ids": [ABSOLUTE]})
        + "\n",
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
        "timestamp": "2026-09-27T00:00:00+00:00",
    }
    path = review_module.review_path(PROJECT, RUN, reviewed_head_sha=HEAD_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")

    review, stale = recovery_module.select_review_for_head(PROJECT, RUN, HEAD_SHA)

    assert stale == ""
    assert review is not None, "the hand-written record must be selected by its head"
    assert review["added_failure_count"] == 0, (
        "the reviewed run added nothing; a count of 1 caps its total below the "
        "promotion floor"
    )
    assert review["added_failure_ids"] == []
    assert review["total"] == TOTAL, "a run that added no failing test is uncapped"
