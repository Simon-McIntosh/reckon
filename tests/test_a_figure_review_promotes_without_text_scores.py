"""A figure review is parsed without the six text scores, and promotes.

A figure review judges the rendered artifact, so it records rubric verdicts
rather than the six text dimensions. The review-store contract required every
text dimension as a key on any parsed record, so a figure reviewer had to write
them, and ``ledger_block`` then called ``int()`` on each value — so the first
live figure review stored its scores as six ``null``s and could not be
promoted: ``crew complete`` raised ``TypeError`` in ``ledger_block``.

The rubric key now decides the measure. A figure record is not required to
carry the text dimensions, whether it omits ``scores`` or holds them as
``null``; a text record is refused for a missing dimension exactly as before.
``ledger_block`` keeps only the dimensions whose value is an integer, so a
figure record reduces to status ``parsed`` with empty scores and no total. That
is the one parsed record that carries no text scores, and a reader tells it
from an unscored text record by its rubric.

Every stored record, pointer and manifest lives under a temporary directory, so
nothing here reads or writes outside it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, pointer_path
from tests.conftest import EXECUTABLE_GATE_COMMAND

PROJECT = "figure-review-promotes-fixture"
PLAN = "figure-review-promotes-target"
SUBJECT = "r-20261020T120000000000-scored-subject"
REVIEW = "r-20261020T121000000000-figure-review"
BASE = "a" * 40
HEAD = "c" * 40
DISPATCH_TS = "2026-10-20T12:00:00Z"


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=tree, check=True, capture_output=True, text=True
    )
    return completed.stdout.strip()


def _figure_record(**overrides: object) -> dict:
    record: dict = {
        "project": PROJECT,
        "reviewed_run_id": SUBJECT,
        "review_run_id": REVIEW,
        "reviewed_base_sha": BASE,
        "reviewed_head_sha": HEAD,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, None),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
    }
    record.update(overrides)
    return record


# -- The store's refusals leave a figure record unscored ----------------------


def test_a_figure_record_with_null_text_scores_passes_the_refusals() -> None:
    """The exact shape the first live figure review stored: six null scores."""
    assert review_module.review_record_refusals(_figure_record()) == []


def test_a_figure_record_with_no_scores_key_passes_the_refusals() -> None:
    """A figure record may omit ``scores`` entirely; the rubric is the measure."""
    record = _figure_record()
    record.pop("scores")
    assert review_module.review_record_refusals(record) == []


def test_a_text_record_missing_a_dimension_is_still_refused() -> None:
    """The text contract is unchanged: a missing dimension is a refusal."""
    record = _figure_record()
    record.pop("rubric")
    record["scores"] = {
        key: value
        for key, value in record["scores"].items()
        if key != review_module.REVIEW_DIMENSIONS[-1]
    }
    refusals = review_module.review_record_refusals(record)
    assert refusals == [f"missing scores.{review_module.REVIEW_DIMENSIONS[-1]}"]


def test_a_fully_scored_text_record_keeps_its_scores_and_total() -> None:
    """The control: a scored text record is untouched by the figure rule."""
    record = _figure_record()
    record.pop("rubric")
    record["scores"] = dict.fromkeys(review_module.REVIEW_DIMENSIONS, 10)
    record["total"] = 60
    assert review_module.review_record_refusals(record) == []
    block = review_module.ledger_block(record)
    assert block is not None
    assert block["scores"] == dict.fromkeys(review_module.REVIEW_DIMENSIONS, 10)
    assert block["total"] == 60


# -- ledger_block reduces a figure record without raising ---------------------


def test_ledger_block_reduces_a_null_score_figure_record() -> None:
    block = review_module.ledger_block(_figure_record())
    assert block is not None
    assert block["status"] == "parsed"
    assert block["scores"] == {}
    assert block["total"] is None


def test_ledger_block_reduces_a_no_scores_figure_record() -> None:
    record = _figure_record()
    record.pop("scores")
    block = review_module.ledger_block(record)
    assert block is not None
    assert block["status"] == "parsed"
    assert block["scores"] == {}
    assert block["total"] is None


# -- A figure review run promotes ---------------------------------------------


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    """A repository whose docs directory is the project's mount."""
    root = tmp_path / "repo"
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / f"{PLAN}.html").write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{PLAN}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n',
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "docs"),
        ("commit", "-q", "-m", "test: seed repository"),
    ):
        _git(root, *arguments)
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _store_figure_round() -> Path:
    """Write the figure review round at the head-keyed figure store path."""
    path = review_module.review_path(
        PROJECT,
        SUBJECT,
        reviewed_head_sha=HEAD,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_figure_record()), encoding="utf-8")
    return path


def _figure_review_pointer(repository: Path, tmp_path: Path, declared: Path) -> None:
    manifest = tmp_path / f"{REVIEW}.manifest.md"
    manifest.write_text(
        "node: review-of-a-subject\n"
        "status: complete\n"
        "commits: none\n"
        "changed_paths: []\n"
        f"tests: {EXECUTABLE_GATE_COMMAND}\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW),
        {
            "run_id": REVIEW,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "launch": "in-harness",
            "backend": "native",
            "created_at": DISPATCH_TS,
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": (f"review-of-{SUBJECT}-{HEAD[:12]}-figure"),
                "plan": PLAN,
                "section": "s1",
                "time_budget": "25m",
                "role": "review",
                "write_paths": [str(declared)],
            },
        },
    )


def test_a_figure_review_run_promotes_with_an_empty_score_block(
    repository: Path, tmp_path: Path
) -> None:
    """The promotion the incident could not reach: no error, empty scores."""
    declared = _store_figure_round()
    _figure_review_pointer(repository, tmp_path, declared)

    crew.complete(REVIEW, gate="passed", root=repository)

    row = json.loads(
        ledger.run_path(PROJECT, REVIEW, root=repository).read_text(encoding="utf-8")
    )
    review_block = row["review"]
    assert review_block is not None, "the row carried no review block"
    assert review_block["status"] == "parsed"
    assert review_block["scores"] == {}
    assert review_block["total"] is None
