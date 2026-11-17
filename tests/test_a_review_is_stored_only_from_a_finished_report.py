"""A review record is stored only from a finished report, in either shape.

A plan reviewer writes its report into the file its dispatch granted, and the
brief hands it the line shapes to emit. A report read while that write is still
under way carries the brief's placeholders where a verdict and a finding go, and
the grammar reads it as a finished review: a placeholder is non-empty text in the
verdict cell, and the FINDING line carries an anchor and no claim. Storing one
took a record with no verdict and no finding back to the plan-review gate, which
is how three real findings never reached one.

The store therefore tells the two states apart by the run that writes the
report. While that reviewer has not finished — its run is live — the report is
held and nothing is stored or refused: the reviewer may still fill it in, and a
refusal recorded now would send a coordinator after a review that is mid-turn. A
later read of the finished report stores the findings it finished with. Once the
reviewer has ended, the placeholders are a delivery defect, named with the tokens
the report still carries, which is what opens a repair round instead of holding
the review open for a writer who has gone.

The second shape is the figure review's record. One figure review judges every
figure its brief attached, so its record states the verdicts per figure; a reader
that read a single flat item map answered a complete record as carrying no
verdict at all, which is the refusal two sessions met. A record whose every
figure carries a verdict for every item delivers, and one figure left unanswered
is refused naming that figure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import _plan_html, crew
from reckon.crew import plan_review
from reckon.crew import review as review_module
from reckon.crew.node import CrewError
from reckon.crew.recovery_review_delivery import review_delivery_refusal
from reckon.crew.runs import _write_json, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "review-finished-report-fixture"
PLAN = "review-finished-report-target"
REVIEWED_RUN_ID = "r-20261115T100000000000-review-finished-report-target"
REVIEW_RUN_ID = "r-20261115T101500000000-review-of-target"
FIGURE_REVIEW_RUN_ID = f"{REVIEW_RUN_ID}-figure"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}-figure"
CREW_RUN_ID = "r-20261115T101400000000-review-finished-report"
DISPATCH_TIME = "2026-11-15T10:14:00Z"
DISPATCHED_HEAD = "a" * 40
REVIEWED_BASE = "c" * 40
FIRST_FIGURE = "docs/figures/review-finished-report-target/first.svg"
SECOND_FIGURE = "docs/figures/review-finished-report-target/second.svg"

# The two placeholder tokens the incident's report still carried. They are the
# shape the brief's own template uses, so a reviewer that pastes the template
# leaves exactly these in place of a verdict and a finding.
PLACEHOLDER_TOKENS = ("RUBRIC-PLACEHOLDER-1", "FINDING-PLACEHOLDER")

PLACEHOLDER_REPORT = (
    "# Plan review\n\n"
    + "".join(
        f"RUBRIC {item}: RUBRIC-PLACEHOLDER-{index}\n"
        for index, item in enumerate(review_module.PLAN_REVIEW_ITEMS, start=1)
    )
    + "FINDING wiring FINDING-PLACEHOLDER\n"
)

FINISHED_REPORT = (
    "# Plan review\n\n"
    + "".join(
        f"RUBRIC {item}: pass — nothing to report.\n"
        for item in review_module.PLAN_REVIEW_ITEMS
    )
    + "FINDING wiring reckon/crew/plan_review.py:1 — the store read a report "
    "whose lines were still the template's — WOULD_CHANGE_THE_PLAN: yes — "
    "REASON: a stored record with no findings is the gate's own blind spot.\n"
    + "FINDING reasoning docs/plans/target.html#s1 — the second finding is "
    "anchored to the section it is about — WOULD_CHANGE_THE_PLAN: yes — "
    "REASON: the section states a done-when that names no measurement.\n"
)


# ── A delivered plan report and the run that writes it ──────────────────────


def _report_directory() -> Path:
    return plan_review.review_report_directory(PROJECT, PLAN, REVIEW_RUN_ID)


def _write_delivery(report_text: str) -> Path:
    """Compose one delivered plan report and the sidecar the store reads."""
    directory = _report_directory()
    directory.mkdir(parents=True, exist_ok=True)
    report = directory / "report.md"
    report.write_text(report_text, encoding="utf-8")
    plan_review.write_review_sidecar(
        directory,
        payload={
            "project": PROJECT,
            "plan_slug": PLAN,
            "plan_version": 1,
            "reviewed_blob_sha": "b" * 40,
            "plan_fingerprint": "f" * 64,
            "section_digests": {},
            "rubric": "content",
            "report_path": str(report),
        },
    )
    _write_json(
        directory / review_module.REVIEW_DISPATCH_FILE_NAME,
        {"run_id": CREW_RUN_ID},
    )
    return directory


def _sidecar(directory: Path) -> dict:
    path = directory / plan_review._REVIEW_SIDECAR_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest_for(tmp_path: Path, status: str) -> Path:
    manifest = tmp_path / "manifests" / f"{CREW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"node: review-of-target\nstatus: {status}\ncheckpoint: reading\n",
        encoding="utf-8",
    )
    return manifest


def _write_live_reviewer(tmp_path: Path) -> None:
    """The crew run writing the report, recording this process as its worker."""
    _write_json(
        pointer_path(CREW_RUN_ID),
        {
            "run_id": CREW_RUN_ID,
            "project": PROJECT,
            "repo": str(tmp_path),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": DISPATCH_TIME,
            "pid": os.getpid(),
            "manifest_path": str(_manifest_for(tmp_path, "in-progress")),
            "node": {"id": "review-of-target", "role": "review"},
        },
    )


def test_a_placeholder_report_is_held_while_its_reviewer_runs(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """A live reviewer's half-written report is read again, never refused."""
    directory = _write_delivery(PLACEHOLDER_REPORT)
    _write_live_reviewer(tmp_path)

    assert plan_review.store_delivered_reviews(PROJECT, PLAN) == []
    assert plan_review.list_plan_reviews(project=PROJECT) == []
    assert "store_error" not in _sidecar(directory)


def test_the_finished_report_stores_its_findings_on_a_later_read(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """The same directory, its report finished, stores the findings it holds."""
    directory = _write_delivery(PLACEHOLDER_REPORT)
    _write_live_reviewer(tmp_path)
    assert plan_review.store_delivered_reviews(PROJECT, PLAN) == []

    (directory / "report.md").write_text(FINISHED_REPORT, encoding="utf-8")
    assert plan_review.store_delivered_reviews(PROJECT, PLAN) == []

    stored = plan_review.list_plan_reviews(project=PROJECT)
    assert len(stored) == 1
    assert [finding["id"] for finding in stored[0]["findings"]] == [
        "wiring-1",
        "reasoning-1",
    ]
    assert stored[0]["review_run_id"] == REVIEW_RUN_ID


def test_a_placeholder_report_is_refused_once_its_reviewer_has_ended(
    isolated_reckon_home: Path, tmp_path: Path
) -> None:
    """Placeholders left by a reviewer that has gone are a delivery defect."""
    directory = _write_delivery(PLACEHOLDER_REPORT)
    _manifest_for(tmp_path, "complete")

    refused = plan_review.store_delivered_reviews(PROJECT, PLAN)

    assert len(refused) == 1, refused
    reason = str(refused[0]["store_error"])
    for token in PLACEHOLDER_TOKENS:
        assert token in reason, (token, reason)
    assert plan_review.list_plan_reviews(project=PROJECT) == []
    recorded = str(_sidecar(directory).get("store_error") or "")
    for token in PLACEHOLDER_TOKENS:
        assert token in recorded, (token, recorded)


# ── The figure record, whose verdicts are per figure ────────────────────────


def _declared_paths() -> list[str]:
    return review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_RUN_ID)),
        REVIEWED_RUN_ID,
        reviewed_head_sha=DISPATCHED_HEAD,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )


def _figure_round(*, unanswered: tuple[str, str] | None = None) -> dict:
    """One figure record, with a verdict from every figure for every item.

    It carries no ``scores`` and no total: a figure review judges the rendered
    artifact, so it records rubric verdicts rather than the six dimensions.
    """
    verdict = "the rendered figure passes this measure"
    figures = {
        FIRST_FIGURE: dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, verdict),
        SECOND_FIGURE: dict.fromkeys(review_module.FIGURE_REVIEW_ITEMS, verdict),
    }
    if unanswered is not None:
        path, item = unanswered
        figures[path].pop(item)
    return {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": FIGURE_REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": DISPATCHED_HEAD,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "figures": figures,
        "findings": [],
        "added_failure_count": 0,
        "added_failure_ids": [],
        "timestamp": "2026-11-15T10:20:00Z",
    }


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
            "title": "Review Finished Report Target",
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
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    (isolated_reckon_home / "projects.json").write_text(
        json.dumps({PROJECT: {"repo": str(root)}}), encoding="utf-8"
    )
    return root


def _write_pointer(repository: Path, tmp_path: Path, declared: list[str]) -> None:
    manifest = tmp_path / "manifests" / f"{FIGURE_REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-figure-target\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(FIGURE_REVIEW_RUN_ID),
        {
            "run_id": FIGURE_REVIEW_RUN_ID,
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


def _stage(repository: Path, tmp_path: Path, body: dict) -> dict:
    declared = _declared_paths()
    _write_pointer(repository, tmp_path, declared)
    target = Path(declared[-1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body), encoding="utf-8")
    Path(run_dir(REVIEWED_RUN_ID)).mkdir(parents=True, exist_ok=True)
    Path(run_dir(FIGURE_REVIEW_RUN_ID)).mkdir(parents=True, exist_ok=True)
    (Path(run_dir(FIGURE_REVIEW_RUN_ID)) / "exit.json").write_text(
        json.dumps({"run_id": FIGURE_REVIEW_RUN_ID, "exit_code": 0}),
        encoding="utf-8",
    )
    return json.loads(pointer_path(FIGURE_REVIEW_RUN_ID).read_text(encoding="utf-8"))


def _refusal(repository: Path) -> str:
    with pytest.raises(CrewError) as raised:
        crew.complete(FIGURE_REVIEW_RUN_ID, gate="not-run", root=repository)
    return str(raised.value)


def test_a_per_figure_record_that_answers_every_figure_delivers(
    repository: Path, tmp_path: Path
) -> None:
    """Every figure's verdict is present, so the record is the review it owed."""
    pointer = _stage(repository, tmp_path, _figure_round())

    assert review_delivery_refusal(pointer, PROJECT) is None

    result = crew.complete(
        FIGURE_REVIEW_RUN_ID,
        gate="not-run",
        root=repository,
        promoted_by="review-self",
    )

    assert result["record"]["run_id"] == FIGURE_REVIEW_RUN_ID
    assert result["record"]["review"]["status"] == "parsed"


def test_a_per_figure_record_missing_a_verdict_names_the_figure(
    repository: Path, tmp_path: Path
) -> None:
    """A figure with no verdict for an item is named, not silently passed."""
    _stage(
        repository,
        tmp_path,
        _figure_round(unanswered=(SECOND_FIGURE, "contrast")),
    )

    refusal = _refusal(repository)

    assert "not a complete review" in refusal, refusal
    assert SECOND_FIGURE in refusal, refusal
    assert "contrast" in refusal, refusal
