"""A dispose refusal names the rubric that holds the record.

A run may be reviewed under more than one rubric, and a figure review is stored
beside the run's text review. ``--review`` names the review run whose record a
disposition answers, and without ``--rubric`` the selection is the text family,
so a figure review named by ``--review`` alone resolves to nothing and the
disposition is refused. The refusal the operator sees must name the family that
does hold the record and the flag value that selects it, because a refusal that
names neither leaves the finding unanswerable through the verb that reported
it: the record is there, in the other family.

Every crew directory resolves inside a temporary configuration home, so nothing
touches the operator's own stores; the module asserts the real store is
untouched by the fixture's project.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import repair as repair_module
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "dispose-rubric-fixture"
SESSION = "coordinator-fixture"
REVIEWED_RUN = "r-20261020T120000000000-reviewed-run"
TEXT_REVIEW_RUN = "r-20261020T130000000000-text-review-run"
FIGURE_REVIEW_RUN = "r-20261020T140000000000-figure-review-run"
ABSENT_REVIEW_RUN = "r-20261020T150000000000-no-such-review"
FOLD_NODE = "fix-a"

# Index 1 of the figure findings is blocking: it is the finding a promotion
# refuses until a disposition answers it.
TEXT_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "reckon/crew/runs.py",
        "line": "10",
        "text": "the retry loop is unbounded",
        "severity": "follow-on",
    },
]
FIGURE_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "docs/figures/p/p.png",
        "line": "1",
        "text": "the axis label is clipped at the canvas edge",
        "severity": "follow-on",
    },
    {
        "file": "docs/figures/p/p.png",
        "line": "2",
        "text": "the caption restates the title",
        "severity": "blocking",
    },
]


def _git(tree: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@dataclass(frozen=True)
class Fixture:
    """The temporary world one assertion reads its report from."""

    config_home: Path
    repository: Path
    head_sha: str
    text_record_path: Path
    figure_record_path: Path


def _record(
    *,
    review_run_id: str,
    head_sha: str,
    findings: list[dict[str, Any]],
    rubric: str | None = None,
) -> dict[str, Any]:
    """One run-review record in the store's own shape, or a figure record."""
    record: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": review_run_id,
        "status": "parsed",
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "findings": findings,
    }
    if rubric is not None:
        record["rubric"] = rubric
    return record


def _seed_checkout(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A git checkout under the temp tree, and its one commit's revision."""
    repository = tmp_path / name
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the fixture checkout"),
    ):
        _git(repository, *arguments)
    return repository, _git(repository, "rev-parse", "HEAD")


def _write_pointer(run_id: str, *, worktree: Path) -> None:
    runs._write_json(
        runs.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "process_alive": False,
            "repo": str(worktree),
            "worktree": str(worktree),
            "node": {
                "id": run_id,
                "plan": "",
                "section": "",
                "time_budget": "20m",
                "write_paths": ["docs"],
            },
        },
    )


def _mount(config_home: Path, repository: Path) -> None:
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A temporary crew home holding one text review and one figure review."""
    repository, head_sha = _seed_checkout(tmp_path, "repo")
    _mount(isolated_reckon_home, repository)
    _write_pointer(REVIEWED_RUN, worktree=repository)

    text_path = review_module.store_review(
        _record(
            review_run_id=TEXT_REVIEW_RUN,
            head_sha=head_sha,
            findings=TEXT_FINDINGS,
        )
    )
    figure_path = review_module.store_review(
        _record(
            review_run_id=FIGURE_REVIEW_RUN,
            head_sha=head_sha,
            findings=FIGURE_FINDINGS,
            rubric=review_module.FIGURE_REVIEW_RUBRIC,
        )
    )
    assert text_path != figure_path
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        head_sha=head_sha,
        text_record_path=text_path,
        figure_record_path=figure_path,
    )


def _dispose(*arguments: str):
    """Invoke the disposition command against the fixture project and run."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            REVIEWED_RUN,
            *arguments,
        ],
    )


def _figure_record() -> dict[str, Any]:
    record = review_module.read_review(
        PROJECT, REVIEWED_RUN, rubric=review_module.FIGURE_REVIEW_RUBRIC
    )
    assert record is not None
    return record


def _blocking_ids() -> list[str]:
    """The ids of the blocking findings the run's figure record still lists."""
    return [
        finding["id"] for finding in repair_module.blocking_findings(_figure_record())
    ]


def test_the_refusal_names_the_rubric_that_holds_the_record(fixture: Fixture) -> None:
    """Naming a figure review without its rubric refuses, naming --rubric figure."""
    blocking_id = repair_module.review_findings({"findings": [FIGURE_FINDINGS[1]]})[0][
        "id"
    ]
    assert _blocking_ids() == [blocking_id]
    figure_before = fixture.figure_record_path.read_text(encoding="utf-8")
    text_before = fixture.text_record_path.read_text(encoding="utf-8")

    result = _dispose(
        "--review",
        FIGURE_REVIEW_RUN,
        "--finding",
        "1",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )

    assert result.exit_code != 0, result.output
    # The refusal names the flag value that selects the family holding the
    # record, so the operator's next call is the one that works.
    assert "--rubric figure" in result.output
    assert FIGURE_REVIEW_RUN in result.output
    # Nothing was written: both records are byte-identical to what they were,
    # and the blocking finding still blocks.
    assert fixture.figure_record_path.read_text(encoding="utf-8") == figure_before
    assert fixture.text_record_path.read_text(encoding="utf-8") == text_before
    assert _blocking_ids() == [blocking_id]


def test_the_named_call_with_the_rubric_disposes_the_finding(
    fixture: Fixture,
) -> None:
    """The same call with --rubric figure answers the figure review's finding."""
    result = _dispose(
        "--review",
        FIGURE_REVIEW_RUN,
        "--rubric",
        "figure",
        "--finding",
        "1",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert result.exit_code == 0, result.output

    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 1
    assert receipt["disposition"] == f"folded:{FOLD_NODE}"
    assert receipt["path"] == str(fixture.figure_record_path)

    stored = json.loads(fixture.figure_record_path.read_text(encoding="utf-8"))
    recorded = stored[review_module.FINDING_DISPOSITIONS_KEY]
    assert recorded["1"]["kind"] == "folded"
    assert recorded["1"]["node"] == FOLD_NODE
    assert _blocking_ids() == []

    # The text record the run also carries is untouched.
    text_stored = json.loads(fixture.text_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in text_stored


def test_a_review_run_with_no_record_keeps_the_existing_refusal(
    fixture: Fixture,
) -> None:
    """A review run that produced no record at all is refused as before."""
    result = _dispose(
        "--review",
        ABSENT_REVIEW_RUN,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert result.exit_code != 0, result.output
    assert "carries no record from review run" in result.output
    # No record was found in any family, so the refusal names no rubric to add.
    assert "--rubric" not in result.output
