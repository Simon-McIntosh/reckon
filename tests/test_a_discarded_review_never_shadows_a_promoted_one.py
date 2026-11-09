"""A discarded review's record never wins the selection at a head.

One run can hold several figure-review records at one head: a review that
figure rubric selection left unparsed and which the coordinator discarded, and
the re-dispatch that measured the figures and was promoted. The by-head
selection ranked only on completeness, so it kept the discarded run's unparsed
record when that filename sorted first; ``crew dispose --review`` then refused,
because the head resolved to a record other than the promoted review the
operator named.

These tests write the records into the committed run directory the selection
reads, put a discard marker where a discard leaves one, and drive the selection
through :func:`stored_record` and through the operator's own ``crew dispose``
and ``crew dispose --review``. Each discarded filename sorts before the record it
must not shadow, so a selection that ignored the discard record would pick it.

Every crew directory resolves inside a temporary configuration home and every
repository is synthesised under the test's own temporary tree, so nothing
touches the operator's stores.
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
from reckon.crew import promotion, runs
from reckon.crew import review as review_module

PROJECT = "discarded-review-fixture"
SESSION = "coordinator-fixture"
REVIEWED_RUN = "r-20261109T090000000000-reviewed-run"
DISCARDED_REVIEW = "r-20261109T100000000000-discarded-figure-review"
PROMOTED_REVIEW = "r-20261109T110000000000-promoted-figure-review"
FOLD_NODE = "fix-a"

# Both names sort with the discarded review first: "1" precedes "1" only by the
# rest of the id, and the promoted id's hour sorts after the discarded one's, so
# a reader that kept the first head-matching file would keep the discarded one.
DISCARDED_NAME = f"{review_module.FIGURE_REVIEW_RUBRIC}.{DISCARDED_REVIEW}.json"
PROMOTED_NAME = f"{review_module.FIGURE_REVIEW_RUBRIC}.{PROMOTED_REVIEW}.json"

FIGURE_FINDINGS: list[dict[str, Any]] = [
    {
        "file": "docs/figures/f/f.png",
        "line": "1",
        "text": "the axis label is clipped at the canvas edge",
        "severity": "follow-on",
    }
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
    run_directory: Path


def _seed_checkout(tmp_path: Path) -> tuple[Path, str]:
    """A git checkout holding the project's committed tree, and its revision."""
    repository = tmp_path / "repo"
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


def _mount(config_home: Path, repository: Path) -> None:
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )


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


def _figure_record(
    *,
    review_run_id: str,
    head_sha: str,
    status: str,
) -> dict[str, Any]:
    """A figure-rubric record in the store's own shape."""
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": review_run_id,
        "status": status,
        "rubric": review_module.FIGURE_REVIEW_RUBRIC,
        "reviewed_base_sha": head_sha,
        "reviewed_head_sha": head_sha,
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": FIGURE_FINDINGS,
    }


def _record_discard(review_run_id: str) -> Path:
    """Leave the marker a discard writes in the review run's own directory."""
    marker = promotion.discard_record_path(review_run_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"run_id": review_run_id, "reason": "fixture discard"}),
        encoding="utf-8",
    )
    return marker


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A temporary crew home and a committed tree holding one reviewed run."""
    repository, head_sha = _seed_checkout(tmp_path)
    _mount(isolated_reckon_home, repository)
    _write_pointer(REVIEWED_RUN, worktree=repository)
    run_directory = (
        repository / "docs" / "state" / PROJECT / "reviews" / "run" / REVIEWED_RUN
    )
    run_directory.mkdir(parents=True)
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        head_sha=head_sha,
        run_directory=run_directory,
    )


def _write(run_directory: Path, name: str, record: dict[str, Any]) -> Path:
    path = run_directory / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _selected(fixture: Fixture) -> tuple[Path | None, dict[str, Any] | None]:
    return review_module.stored_record(
        PROJECT,
        REVIEWED_RUN,
        reviewed_head_sha=fixture.head_sha,
        rubric=review_module.FIGURE_REVIEW_RUBRIC,
    )


def _dispose(*arguments: str):
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


def test_a_discarded_unparsed_record_never_shadows_a_promoted_parsed_one(
    fixture: Fixture,
) -> None:
    """The unparsed record from a discarded run loses to the promoted one."""
    # Pinned: the discarded filename sorts before the record it must not shadow,
    # so a selection that ranked only on completeness would keep the discarded
    # record rather than reach the parsed one.
    assert DISCARDED_NAME < PROMOTED_NAME
    discarded = _write(
        fixture.run_directory,
        DISCARDED_NAME,
        _figure_record(
            review_run_id=DISCARDED_REVIEW,
            head_sha=fixture.head_sha,
            status="unparsed",
        ),
    )
    promoted = _write(
        fixture.run_directory,
        PROMOTED_NAME,
        _figure_record(
            review_run_id=PROMOTED_REVIEW,
            head_sha=fixture.head_sha,
            status="parsed",
        ),
    )
    assert _record_discard(DISCARDED_REVIEW)

    path, record = _selected(fixture)

    assert record is not None
    assert Path(path).resolve() == promoted.resolve()
    assert record["review_run_id"] == PROMOTED_REVIEW
    assert record["status"] == "parsed"
    assert Path(path).resolve() != discarded.resolve()

    # The operator's verb reaches the promoted record: before the selection
    # skipped the discarded run, this call refused because the head resolved to
    # the discarded record.
    result = _dispose(
        "--review",
        PROMOTED_REVIEW,
        "--rubric",
        review_module.FIGURE_REVIEW_RUBRIC,
        "--finding",
        "0",
        "--kind",
        "folded",
        "--node",
        FOLD_NODE,
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["path"] == str(promoted.resolve())

    stored = json.loads(promoted.read_text(encoding="utf-8"))
    recorded = stored[review_module.FINDING_DISPOSITIONS_KEY]
    assert recorded["0"]["kind"] == "folded"
    assert recorded["0"]["node"] == FOLD_NODE

    # The discarded record was never written to.
    untouched = json.loads(discarded.read_text(encoding="utf-8"))
    assert review_module.FINDING_DISPOSITIONS_KEY not in untouched


def test_a_discarded_parsed_record_never_shadows_a_promoted_parsed_one(
    fixture: Fixture,
) -> None:
    """A discarded run loses to a promoted one even when both parsed cleanly."""
    assert DISCARDED_NAME < PROMOTED_NAME
    _write(
        fixture.run_directory,
        DISCARDED_NAME,
        _figure_record(
            review_run_id=DISCARDED_REVIEW,
            head_sha=fixture.head_sha,
            status="parsed",
        ),
    )
    promoted = _write(
        fixture.run_directory,
        PROMOTED_NAME,
        _figure_record(
            review_run_id=PROMOTED_REVIEW,
            head_sha=fixture.head_sha,
            status="parsed",
        ),
    )
    _record_discard(DISCARDED_REVIEW)

    path, record = _selected(fixture)

    assert record is not None
    assert Path(path).resolve() == promoted.resolve()
    assert record["review_run_id"] == PROMOTED_REVIEW


def test_a_lone_unparsed_record_still_resolves(fixture: Fixture) -> None:
    """With nothing to displace it, an unparsed record is still read."""
    lone = _write(
        fixture.run_directory,
        DISCARDED_NAME,
        _figure_record(
            review_run_id=DISCARDED_REVIEW,
            head_sha=fixture.head_sha,
            status="unparsed",
        ),
    )

    path, record = _selected(fixture)

    assert record is not None
    assert Path(path).resolve() == lone.resolve()
    assert record["status"] == "unparsed"


def test_the_fixture_home_is_temporary_and_the_real_store_is_untouched(
    fixture: Fixture,
) -> None:
    """The store resolved is the temporary home, and the operator's gains nothing."""
    resolved_store = review_module.review_store_root()
    assert resolved_store.is_relative_to(fixture.config_home)
    real_project_dir = Path.home() / ".config" / "reckon" / "crew" / "reviews" / PROJECT
    assert not real_project_dir.exists(), (
        f"a fixture write escaped to the operator's store at {real_project_dir}"
    )
