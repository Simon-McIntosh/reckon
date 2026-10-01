"""A gate log's revision header is read in each spelling the store carries.

Three spellings appear on the first line of arm logs: the conventional
``revision <sha> tree <path>``, a promotion replay's ``revision: <sha>``, and
``revision=<sha>``, which the reader did not accept — so for those logs the
annotated review's own revision key stayed null and the added-failure count
could not be tied to the commit it measured. These cases pin the reader and
the annotation path that consumes it to all three forms.

Every crew directory is environment-resolved under ``tmp_path``; nothing
touches the operator's own store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew import recovery as recovery_module
from reckon.crew import review as review_module
from reckon.crew.runs import run_dir

PROJECT = "proj"
RUN = "r-reviewed-run"
BASE_SHA = "a" * 40
HEAD_SHA = "3c8f9e4c" + "d" * 32
A = "tests/test_gate.py::test_alpha"
B = "tests/test_gate.py::test_beta"
SCORE = 18
TOTAL = SCORE * len(review_module.REVIEW_DIMENSIONS)


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every crew directory at a temporary home for this test."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _log(failed: list[str], *, first_line: str) -> str:
    lines = [first_line]
    lines.extend(f"FAILED {test_id} - AssertionError: boom" for test_id in failed)
    lines.append(f"{len(failed)} failed")
    return "\n".join(lines) + "\n"


def _manifest_text() -> str:
    return (
        "\n".join(
            [
                f"node: {RUN}",
                "status: complete",
                "baseline_suite: "
                + json.dumps({"log_path": "base.log", "failure_ids": [A]}),
                "after_suite: "
                + json.dumps({"log_path": "head.log", "failure_ids": [A, B]}),
            ]
        )
        + "\n"
    )


def _synthesise_run() -> None:
    directory = run_dir(RUN)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "base.log").write_text(
        _log([A], first_line=f"revision {BASE_SHA} tree /base"), encoding="utf-8"
    )
    (directory / "head.log").write_text(
        _log([A, B], first_line=f"revision={HEAD_SHA} tree /head"), encoding="utf-8"
    )
    (directory / "manifest.md").write_text(_manifest_text(), encoding="utf-8")


def _write_review() -> None:
    record = {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, SCORE),
        "absent": [],
        "total": TOTAL,
        review_module.REVIEWED_HEAD_KEY: HEAD_SHA,
        "timestamp": "2026-09-30T08:00:00+00:00",
    }
    path = review_module.review_path(PROJECT, RUN, reviewed_head_sha=HEAD_SHA)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


@pytest.mark.parametrize(
    "first_line",
    [
        f"revision {HEAD_SHA} tree /tree",
        f"revision: {HEAD_SHA} tree /tree",
        f"revision={HEAD_SHA} tree /tree",
    ],
    ids=["space", "colon", "equals"],
)
def test_each_first_line_form_yields_its_sha(first_line: str) -> None:
    assert review_module.recorded_log_revision(first_line) == HEAD_SHA


def test_a_revision_shaped_token_after_the_first_line_is_not_read() -> None:
    text = f"pytest run\nrevision={HEAD_SHA} tree /tree\n"
    assert review_module.recorded_log_revision(text) is None


def test_a_revision_equals_head_log_sets_the_head_log_revision_key(
    crew_home: Path,
) -> None:
    _write_review()
    _synthesise_run()

    review, stale = recovery_module.select_review_for_head(PROJECT, RUN, HEAD_SHA)

    assert stale == ""
    assert review is not None, "the hand-written record must be selected by its head"
    assert review["head_log_revision"] == HEAD_SHA, (
        "the equals form's sha must reach the annotated review, where the "
        "added-failure count is tied to the commit it measured"
    )
    assert review["base_log_revision"] == BASE_SHA
