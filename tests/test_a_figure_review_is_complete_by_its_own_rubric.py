"""A figure review is complete by the measures of its own rubric.

A figure review records qualitative rubric verdicts instead of the six
landed-node score dimensions, and the delivery check judged every record by the
code-review measures alone. So a figure record that carried every measure its
own rubric asks for was refused at its first promotion as an incomplete review,
naming six dimensions it was never asked for — 5 to 8 minutes of review spent
again with ``--outcome`` on each one.

The delivery check now reads a record's measures by its rubric, through the
same rubric reader ``crew check --review`` applies: a figure record is complete
when it carries a verdict for every item of ``FIGURE_REVIEW_ITEMS``, and a text
record is judged as before. A verdict line that states nothing is not a
verdict, so an empty one leaves its item incomplete.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.recovery_review_delivery import review_delivery_refusal
from reckon.crew.recovery_review_subject import (
    _review_is_complete,
    _review_missing_measures,
)
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "figure-completeness-fixture"
PLAN = "figure-completeness-target"
REVIEWED_RUN_ID = "r-20261021T131411862066-figure-target"
FIGURE_REVIEW_RUN_ID = f"{REVIEWED_RUN_ID}-review-of-figure-target-figure"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}-figure"
DISPATCHED_HEAD = "a" * 40
REVIEWED_BASE = "c" * 40
DISPATCH_TIME = "2026-10-21T13:14:11Z"

# The one figure measure each incomplete case withholds, and the code dimension
# a text record withholds in the control case.
MISSING_FIGURE_ITEM = review_module.FIGURE_REVIEW_ITEMS[-1]
MISSING_TEXT_DIMENSION = review_module.REVIEW_DIMENSIONS[-1]


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A committed repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Figure Completeness Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}),
        encoding="utf-8",
    )
    (isolated_reckon_home / "projects.json").write_text(
        json.dumps({PROJECT: {"repo": str(root)}}), encoding="utf-8"
    )
    return root


def _declared_paths() -> list[str]:
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )


def _figure_round(*, missing_item: str | None = None, empty_item: str | None = None):
    """A figure record carrying a verdict for every measure of its rubric.

    It carries no ``scores`` and no total: a figure review judges the rendered
    artifact, so it records rubric verdicts rather than the six dimensions.
    """
    verdicts = dict.fromkeys(
        review_module.FIGURE_REVIEW_ITEMS, "the rendered figure passes this measure"
    )
    if missing_item is not None:
        verdicts.pop(missing_item)
    if empty_item is not None:
        verdicts[empty_item] = "   "
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": FIGURE_REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "rubric_items": verdicts,
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "timestamp": "2026-10-21T13:20:00Z",
    }


def _text_round(*, missing_dimension: str | None = None):
    """A code review record, scored on the six landed-node dimensions."""
    scores = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20)
    if missing_dimension is not None:
        scores.pop(missing_dimension)
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": "r-20261021T132000000000-review-of-figure-target",
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "scores": scores,
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "timestamp": "2026-10-21T13:20:00Z",
    }


def _write_pointer(repository: Path, tmp_path: Path, run_id: str, declared: list[str]):
    manifest = tmp_path / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-figure-target\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": DISPATCH_TIME,
            "manifest_path": str(manifest),
            "node": {
                "id": REVIEW_NODE_ID,
                "plan": PLAN,
                "section": "s1",
                "time_budget": "25m",
                "role": "review",
                "write_paths": declared,
            },
        },
    )


def _write_record(path: str, body: dict) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body), encoding="utf-8")
    return target


def _stage(repository: Path, tmp_path: Path, body: dict, *, run_id: str) -> dict:
    declared = _declared_paths()
    _write_pointer(repository, tmp_path, run_id, declared)
    _write_record(declared[-1], body)
    Path(run_dir(run_id)).mkdir(parents=True, exist_ok=True)
    (Path(run_dir(run_id)) / "exit.json").write_text(
        json.dumps({"run_id": run_id, "exit_code": 0}), encoding="utf-8"
    )
    return json.loads(pointer_path(run_id).read_text(encoding="utf-8"))


def _refusal(repository: Path, run_id: str) -> str:
    with pytest.raises(CrewError) as raised:
        crew.complete(run_id, gate="not-run", root=repository)
    return str(raised.value)


def test_a_figure_record_carrying_every_figure_measure_delivers_and_promotes(
    repository: Path, tmp_path: Path
) -> None:
    """The record the incident could not promote: all six measures, no scores."""
    body = _figure_round()
    # The record the delivery check judges complete is the record
    # ``crew check --review`` accepts, and the fleet's own completeness reader
    # gives the same answer: one rule, not two.
    assert review_module.review_record_refusals(body) == []
    assert _review_is_complete(body) is True
    pointer = _stage(repository, tmp_path, body, run_id=FIGURE_REVIEW_RUN_ID)

    assert review_delivery_refusal(pointer, PROJECT) is None

    result = crew.complete(
        FIGURE_REVIEW_RUN_ID,
        gate="not-run",
        root=repository,
        promoted_by="review-self",
    )

    review = result["record"]["review"]
    assert review["status"] == "parsed"
    assert review["scores"] == {}
    assert review["total"] is None
    assert result["record"]["run_id"] == FIGURE_REVIEW_RUN_ID


@pytest.mark.parametrize("item", review_module.FIGURE_REVIEW_ITEMS)
def test_a_figure_record_missing_a_measure_is_named_for_that_measure(
    repository: Path, tmp_path: Path, item: str
) -> None:
    """An incomplete figure record names the figure measure it lacks."""
    _stage(
        repository,
        tmp_path,
        _figure_round(missing_item=item),
        run_id=FIGURE_REVIEW_RUN_ID,
    )

    refusal = _refusal(repository, FIGURE_REVIEW_RUN_ID)

    assert "not a complete review" in refusal, refusal
    assert item in refusal, refusal
    for dimension in review_module.REVIEW_DIMENSIONS:
        assert dimension not in refusal, (item, dimension, refusal)


def test_a_figure_measure_that_states_nothing_is_still_missing(
    repository: Path, tmp_path: Path
) -> None:
    """A verdict line that says nothing is not a verdict."""
    _stage(
        repository,
        tmp_path,
        _figure_round(empty_item=MISSING_FIGURE_ITEM),
        run_id=FIGURE_REVIEW_RUN_ID,
    )

    refusal = _refusal(repository, FIGURE_REVIEW_RUN_ID)

    assert MISSING_FIGURE_ITEM in refusal, refusal


def test_a_complete_code_record_is_judged_as_today(
    repository: Path, tmp_path: Path
) -> None:
    """The code rubric keeps its six dimensions and its derived total."""
    text_run_id = "r-20261021T132000000000-review-of-figure-target"
    pointer = _stage(repository, tmp_path, _text_round(), run_id=text_run_id)

    assert review_delivery_refusal(pointer, PROJECT) is None

    result = crew.complete(
        text_run_id, gate="not-run", root=repository, promoted_by="review-self"
    )

    review = result["record"]["review"]
    assert review["scores"] == dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20)
    assert review["total"] == 20 * len(review_module.REVIEW_DIMENSIONS)


def test_an_incomplete_code_record_is_refused_for_its_own_measure(
    repository: Path, tmp_path: Path
) -> None:
    """A text record is still refused naming the dimension it never scored."""
    text_run_id = "r-20261021T132000000000-review-of-figure-target"
    _stage(
        repository,
        tmp_path,
        _text_round(missing_dimension=MISSING_TEXT_DIMENSION),
        run_id=text_run_id,
    )

    refusal = _refusal(repository, text_run_id)

    assert "not a complete review" in refusal, refusal
    assert MISSING_TEXT_DIMENSION in refusal, refusal


def _verdict_line(item: str, verdict: str = "pass") -> str:
    return f"RUBRIC {item}: {verdict} - the rendered artifact was read for {item}."


def _line_stated_round(*, missing_item: str | None = None) -> dict:
    """A figure record stating its verdicts as lines and a summary map only.

    It carries no ``rubric_items``: a reviewer wrote the verdict lines its brief
    asked for and a ``rubric_verdicts`` summary beside them, and the record was
    stored as written. A line is also present for an item outside the rubric,
    as a real record carries, so the fold must ignore what it was not asked for.
    """
    items = [item for item in review_module.FIGURE_REVIEW_ITEMS if item != missing_item]
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": FIGURE_REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "rubric_lines": [_verdict_line(item) for item in items]
        + [_verdict_line("caption_not_title_repeat")],
        "rubric_verdicts": dict.fromkeys(items, "pass"),
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "timestamp": "2026-10-21T13:20:00Z",
    }


def test_a_figure_record_stating_every_verdict_line_is_complete() -> None:
    """The record the promotion gate refused: six verdict lines, no item map."""
    body = _line_stated_round()

    assert _review_missing_measures(body) == []
    assert _review_is_complete(body) is True


def test_a_figure_record_missing_a_verdict_line_names_only_that_item() -> None:
    """Five verdict lines report that one item and no other."""
    missing = review_module.FIGURE_REVIEW_ITEMS[-1]
    body = _line_stated_round(missing_item=missing)

    assert _review_missing_measures(body) == [missing]
    assert _review_is_complete(body) is False


def test_a_figure_record_with_an_item_map_reads_as_before() -> None:
    """A flat item map is still judged by its own entries."""
    complete = _figure_round()
    assert _review_missing_measures(complete) == []

    missing = review_module.FIGURE_REVIEW_ITEMS[-1]
    incomplete = _figure_round(missing_item=missing)
    assert _review_missing_measures(incomplete) == [missing]


def test_the_figure_lines_are_read_by_the_one_rubric_grammar() -> None:
    """The figure lines read the module's single compiled RUBRIC pattern."""
    assert not hasattr(review_module, "FIGURE_LINE_RE")
    body = {"rubric_lines": ["RUBRIC   Legibility  :  pass - spacing tolerated"]}

    assert review_module.figure_line_verdicts(body) == {
        "legibility": "pass - spacing tolerated"
    }


def test_a_verdict_line_does_not_overwrite_a_recorded_item_verdict() -> None:
    """A line adds no verdict to an item the record already answers."""
    body = _figure_round()
    body["rubric_lines"] = ["RUBRIC legibility: defect - a contradictory line"]

    answers, _shortfalls = review_module.figure_rubric_verdicts(body)

    assert answers["legibility"] == "the rendered figure passes this measure"
    assert _review_missing_measures(body) == []


def test_a_verdict_line_answers_an_item_the_map_leaves_blank() -> None:
    """A blank map entry is not an answer, so the line's verdict is read."""
    body = _figure_round()
    body["rubric_items"]["contrast"] = "   "
    body["rubric_lines"] = ["RUBRIC Contrast: pass - read as near-black on white"]

    assert _review_missing_measures(body) == []


def test_a_per_figure_record_answered_in_lines_is_complete() -> None:
    """A line answers its item for every figure the record names."""
    missing = review_module.FIGURE_REVIEW_ITEMS[-1]
    per_figure = dict.fromkeys(
        review_module.FIGURE_REVIEW_ITEMS, "the rendered figure passes this measure"
    )
    per_figure.pop(missing)
    body = _figure_round()
    body.pop("rubric_items")
    body["figures"] = {"a.png": dict(per_figure), "b.png": dict(per_figure)}
    body["rubric_lines"] = [_verdict_line(missing)]

    answers, shortfalls = review_module.figure_rubric_verdicts(body)

    assert answers.get(missing) == _verdict_line(missing).split(": ", 1)[1]
    assert shortfalls == []
    assert _review_missing_measures(body) == []


def test_a_figure_record_filed_under_the_reviewers_own_key_is_complete() -> None:
    """A flat map filed as ``rubric_verdicts`` answers the rubric.

    Two reviewers on two projects, told only to include the rubric verdicts,
    filed the six items under that name with no ``rubric_items`` and no verdict
    lines, and each record was refused as lacking all six.
    """
    body = {
        "status": "parsed",
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "rubric_verdicts": dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, "pass"),
    }
    assert _review_missing_measures(body) == []
    missing = review_module.FIGURE_REVIEW_ITEMS[0]
    del body["rubric_verdicts"][missing]
    assert _review_missing_measures(body) == [missing]


def test_the_figure_brief_names_the_key_the_gate_reads() -> None:
    """The reviewer is told the key by name rather than left to choose one."""
    brief = (
        Path(review_module.__file__).parent / "prompts" / "figure_review.md"
    ).read_text(encoding="utf-8")
    assert '"rubric_items"' in brief
