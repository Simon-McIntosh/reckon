"""``crew dispose`` answers a finding on a run's figure review.

A run is reviewed under more than one rubric, and the figure review is stored
beside the run's text review rather than in place of it, so a finding the figure
reviewer raised lives on a record the disposition writer never selected:
``stored_record`` without a rubric returns text-review records only, and the
command's receipt read the same text-only selection. A wrong figure finding was
therefore unanswerable through the operator's own verb. This module exercises
the seam through that verb — ``crew dispose --rubric figure`` — and shows the
disposition landing on the figure record, the receipt composed from it, the
finding leaving ``repair.blocking_findings``, and the text record untouched.

Every crew directory resolves inside a temporary configuration home, so nothing
touches the operator's own stores; the module asserts the real store gains no
directory for the fixture project.
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

PROJECT = "figure-disposition-fixture"
SESSION = "coordinator-fixture"
REVIEWED_RUN = "r-20261020T120000000000-reviewed-run"
TEXT_REVIEW_RUN = "r-20261020T130000000000-text-review-run"
FIGURE_REVIEW_RUN = "r-20261020T140000000000-figure-review-run"
FOLD_NODE = "fix-a"

# The findings each record carries. Index 1 is the blocking one on the figure
# record: it is what promotion refuses until a disposition answers it.
TEXT_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "reckon/crew/runs.py",
        "line": "10",
        "text": "the retry loop is unbounded",
        "severity": "follow-on",
    },
    {
        "file": "reckon/crew/query.py",
        "line": "20",
        "text": "the refusal names the wrong field",
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
    """A temporary crew home holding one text review and one figure review.

    The reviewed run's pointer names the synthesised checkout the fixture
    seeds, so a record carrying a revision resolves it against a real commit
    rather than being refused by the store's own write check.
    """
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


def _figure_record(fixture: Fixture) -> dict[str, Any]:
    record = review_module.read_review(
        PROJECT, REVIEWED_RUN, rubric=review_module.FIGURE_REVIEW_RUBRIC
    )
    assert record is not None
    return record


def _blocking_ids(fixture: Fixture) -> list[str]:
    """The ids of the blocking findings the run's figure record still lists."""
    return [
        finding["id"]
        for finding in repair_module.blocking_findings(_figure_record(fixture))
    ]


def test_the_figure_finding_is_disposed_onto_the_figure_record(
    fixture: Fixture,
) -> None:
    """The disposition lands on the figure record, and the finding stops blocking."""
    blocking_id = repair_module.review_findings({"findings": [FIGURE_FINDINGS[1]]})[0][
        "id"
    ]
    assert _blocking_ids(fixture) == [blocking_id]

    result = _dispose(
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
    # The finding the figure review raised is no longer blocking: this is the
    # list a promotion's refusal is built from, so an empty one is the answer.
    assert _blocking_ids(fixture) == []

    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 1
    assert receipt["disposition"] == f"folded:{FOLD_NODE}"
    assert receipt["path"] == str(fixture.figure_record_path)

    stored = json.loads(fixture.figure_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY in stored
    recorded = stored[review_module.FINDING_DISPOSITIONS_KEY]
    assert recorded["1"]["kind"] == "folded"
    assert recorded["1"]["node"] == FOLD_NODE
    # The record's own review material survives the rewrite, and the receipt's
    # answer reads back through the same rubric-selected reader a consumer uses.
    # The store stamps each finding with the id an answer names, so the record
    # reads back as written apart from that stamp, which is derived here from
    # what was written rather than compared against nothing.
    assert stored["findings"] == [
        {**finding, "id": repair_module.finding_id(finding)}
        for finding in FIGURE_FINDINGS
    ]
    assert (
        _figure_record(fixture)[review_module.FINDING_DISPOSITIONS_KEY]["1"]["node"]
        == FOLD_NODE
    )

    # The disposition answered the figure record and nothing else: the run's
    # text review is unchanged.
    text_stored = json.loads(fixture.text_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in text_stored


def test_the_same_command_without_a_rubric_writes_the_text_record(
    fixture: Fixture,
) -> None:
    """Omitting the rubric answers the run's text review, exactly as before."""
    result = _dispose("--finding", "0", "--kind", "exempted", "--reason", "carried")
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 0
    assert receipt["disposition"] == "exempted:carried"
    assert receipt["path"] == str(fixture.text_record_path)

    text_stored = json.loads(fixture.text_record_path.read_text(encoding="utf-8"))
    assert (
        text_stored[review_module.FINDING_DISPOSITIONS_KEY]["0"]["kind"] == "exempted"
    )
    figure_stored = json.loads(fixture.figure_record_path.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in figure_stored


def test_a_run_with_no_figure_record_is_refused_naming_the_absence(
    tmp_path: Path, isolated_reckon_home: Path
) -> None:
    """A run carrying no figure review names that absence rather than writing."""
    repository, head_sha = _seed_checkout(tmp_path, "text-only-repo")
    _mount(isolated_reckon_home, repository)
    review_module.store_review(
        _record(
            review_run_id=TEXT_REVIEW_RUN,
            head_sha=head_sha,
            findings=TEXT_FINDINGS,
        )
    )

    result = _dispose(
        "--rubric", "figure", "--finding", "0", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code != 0, result.output
    assert "carries no figure review record" in result.output
    assert PROJECT in result.output


def test_the_dispose_help_names_the_rubric_option() -> None:
    """The operator can find the option that reaches a figure review."""
    result = CliRunner().invoke(cli_main, ["crew", "dispose", "--help"])
    assert result.exit_code == 0, result.output
    assert "--rubric" in result.output


def test_the_fixture_home_is_temporary_and_the_real_store_is_untouched(
    fixture: Fixture,
) -> None:
    """The store resolved is the temporary home, and the operator's gains nothing."""
    resolved_store = review_module.review_store_root()
    assert (
        fixture.config_home in resolved_store.parents
        and resolved_store.is_relative_to(fixture.config_home)
    )

    real_project_dir = Path.home() / ".config" / "reckon" / "crew" / "reviews" / PROJECT
    assert not real_project_dir.exists(), (
        f"a fixture write escaped to the operator's store at {real_project_dir}"
    )
