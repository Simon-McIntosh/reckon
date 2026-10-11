"""A code review's finding is disposable and readable by the path it names.

A code review is stored keyed by the neighbourhood it read, under its own file
family beside the plan reviews, and its run id carries the store's own prefix.
Nothing that answers or reads a finding reached that record: ``crew dispose``
resolved only run-review records, and the findings reader walked only the
dimension and plan trees. This module exercises the seam through the operator's
own entry point — the CLI dispose verb and the findings reader — and shows that a
finding on a stored code review is disposed by any of the three identities its
run is known by (the dispatched run id, the composer's run id and the
``code-<key>`` stem), that the entry lands in the code review's own record, and
that the findings reader lists both its findings under the paths they name with
the disposed one marked.

Every crew directory resolves inside a temporary configuration home, so nothing
touches the operator's own stores; the module asserts the fixture's home is not
the workstation's and that the real store gains no directory for the fixture
project.
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
from reckon.crew import findings as findings_module
from reckon.crew import plan_review as plan_review_module
from reckon.crew import review as review_module
from reckon.crew import runs

PROJECT = "code-finding-disposable-fixture"
SESSION = "coordinator-fixture"

# The neighbourhood key a code review is keyed by, and the two run ids that
# carry it: the id the review node was dispatched under and the composer's id.
KEY = "98773e8b1367"
DISPATCHED_RUN = f"r-20261011T205901336241-review-code-{KEY}"
COMPOSER_RUN = f"r-20261011T205859790591-review-code-{KEY}"
CODE_STEM = f"code-{KEY}"
REVISION = "d" * 40

# The two paths the two findings name, and the reviewed run a run review answers.
PATH_A = "reckon/crew/review.py"
PATH_B = "reckon/crew/findings.py"
# The run review's findings name different paths, so the two populations' rows
# never collide in the reader's per-path groups.
PATH_C = "reckon/crew/runs.py"
PATH_D = "reckon/crew/obligations.py"
FOLD_NODE = "fix-a"
REVIEWED_RUN = "r-20261010T090000000000-reviewed-run"
RUN_REVIEW_RUN = "r-20261010T100000000000-review-run"

OPEN = "open"


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
    code_record_path: Path
    run_review_file: Path


def _code_findings() -> list[dict[str, Any]]:
    """Two code findings, each naming a repository path and a line."""
    return [
        {
            "anchor": f"{PATH_A}:10",
            "cites": "new.",
            "class": "duplicated_capability",
            "id": "duplicate_capability-1",
            "item": "duplicate_capability",
            "text": "the moments are re-derived beside the owner that already holds them",
        },
        {
            "anchor": f"{PATH_B}:20",
            "cites": "new.",
            "class": "interface_growth",
            "id": "interface_growth-1",
            "item": "interface_growth",
            "text": "a second read surface grew beside the incumbent without a retirement",
        },
    ]


def _store_code_review(project: str) -> Path:
    """Store one code review holding the two findings, through the store's writer."""
    return plan_review_module.store_code_review(
        {
            "project": project,
            "subject": "code",
            "neighbourhood_key": KEY,
            "review_run_id": DISPATCHED_RUN,
            "reviewed_revision": REVISION,
            "named_paths": [PATH_A],
            "modules": [PATH_A, PATH_B],
            "rubric": "health",
            "status": "ready",
            "findings": _code_findings(),
        }
    )


def _run_review_findings() -> list[dict[str, str]]:
    """Two run-review findings in the store's own ``file``/``line`` spelling."""
    return [
        {
            "file": PATH_C,
            "line": "10",
            "text": "the guard is reachable with an empty index",
            "severity": "follow-on",
        },
        {
            "file": PATH_D,
            "line": "20",
            "text": "the refusal names the wrong field",
            "severity": "follow-on",
        },
    ]


def _run_review_record(head_sha: str) -> dict[str, Any]:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": RUN_REVIEW_RUN,
        "status": "parsed",
        "reviewed_base_sha": "a" * 40,
        "reviewed_head_sha": head_sha,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 18),
        "findings": _run_review_findings(),
        "total": 18 * len(review_module.REVIEW_DIMENSIONS),
    }


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


@pytest.fixture()
def fixture(tmp_path: Path, isolated_reckon_home: Path) -> Fixture:
    """A temporary crew home holding one stored code review and one run review."""
    repository = tmp_path / "repo"
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed a code-finding fixture"),
    ):
        _git(repository, *arguments)
    head_sha = _git(repository, "rev-parse", "HEAD")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )

    _write_pointer(REVIEWED_RUN, worktree=repository)
    # A dispatched review run carries a live pointer like any run; the code
    # review's record is still reached by its neighbourhood key, not this head.
    _write_pointer(DISPATCHED_RUN, worktree=repository)
    run_review_file = review_module.store_committed_review(
        _run_review_record(head_sha), root=repository
    )
    code_record_path = _store_code_review(PROJECT)
    return Fixture(
        config_home=isolated_reckon_home,
        repository=repository,
        head_sha=head_sha,
        code_record_path=code_record_path,
        run_review_file=run_review_file,
    )


def _dispose(fixture: Fixture, run_id: str, *arguments: str):
    """Invoke the disposition command against ``run_id`` through the CLI."""
    return CliRunner().invoke(
        cli_main,
        [
            "crew",
            "dispose",
            "--project",
            PROJECT,
            "--run",
            run_id,
            *arguments,
        ],
    )


def _dispositions_by_path(fixture: Fixture) -> dict[tuple[str, int], str]:
    """The disposition of each finding, keyed by its path and index."""
    report = findings_module.findings_by_path(PROJECT, root=fixture.repository)
    return {(row["path"], row["finding"]): row["disposition"] for row in report["rows"]}


DISPOSED_BY = (
    ("dispatched", DISPATCHED_RUN),
    ("composer", COMPOSER_RUN),
    ("code-key", CODE_STEM),
)


@pytest.mark.parametrize(
    "run_id", [case[1] for case in DISPOSED_BY], ids=[case[0] for case in DISPOSED_BY]
)
def test_a_code_finding_is_disposed_by_each_identity_its_run_carries(
    fixture: Fixture, run_id: str
) -> None:
    """Every identity the code review's run is known by resolves to its record."""
    result = _dispose(
        fixture, run_id, "--finding", "1", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["finding_index"] == 1
    assert receipt["disposition"] == f"folded:{FOLD_NODE}"
    assert receipt["path"] == str(fixture.code_record_path)

    stored = json.loads(fixture.code_record_path.read_text(encoding="utf-8"))
    recorded = stored[review_module.FINDING_DISPOSITIONS_KEY]
    assert recorded["1"]["kind"] == "folded"
    assert recorded["1"]["node"] == FOLD_NODE
    assert review_module.DIMENSION_DISPOSITIONS_KEY not in stored
    # The record's own review material is preserved by the rewrite.
    assert len(stored["findings"]) == 2


def test_the_findings_reader_lists_a_code_review_by_path_with_its_disposition(
    fixture: Fixture,
) -> None:
    """Both code findings appear under the paths they name; the disposed one is marked."""
    result = _dispose(
        fixture,
        DISPATCHED_RUN,
        "--finding",
        "1",
        "--kind",
        "exempted",
        "--reason",
        "carried by the merged head",
    )
    assert result.exit_code == 0, result.output

    by_path = _dispositions_by_path(fixture)
    assert by_path[(PATH_A, 0)] == OPEN
    assert by_path[(PATH_B, 1)] == "exempted:carried by the merged head"

    report = findings_module.findings_by_path(PROJECT, root=fixture.repository)
    grouped = {group["path"]: group for group in report["paths"]}
    assert PATH_A in grouped and PATH_B in grouped
    assert grouped[PATH_A]["rows"][0]["disposition"] == OPEN
    assert grouped[PATH_B]["rows"][0]["disposition"].startswith("exempted:")


def test_a_dimension_disposition_on_a_code_review_is_refused_by_name(
    fixture: Fixture,
) -> None:
    """A code review scores no dimensions, so ``--dimension`` names one it lacks."""
    before = fixture.code_record_path.read_bytes()

    result = _dispose(
        fixture,
        DISPATCHED_RUN,
        "--dimension",
        "durability",
        "--kind",
        "exempted",
        "--reason",
        "no dimensions here",
    )

    assert result.exit_code != 0, result.output
    assert "scores no dimensions" in result.output
    assert fixture.code_record_path.read_bytes() == before


def test_a_run_review_dispose_and_listing_are_unchanged(fixture: Fixture) -> None:
    """The run-review path still disposes and still lists, as it did before."""
    result = _dispose(
        fixture, REVIEWED_RUN, "--finding", "1", "--kind", "folded", "--node", FOLD_NODE
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["disposition"] == f"folded:{FOLD_NODE}"

    by_path = _dispositions_by_path(fixture)
    assert by_path[(PATH_C, 0)] == OPEN
    assert by_path[(PATH_D, 1)] == f"folded:{FOLD_NODE}"


def test_the_fixture_home_is_temporary_and_the_real_store_is_untouched(
    fixture: Fixture,
) -> None:
    """The store resolved is the temporary home, and the operator's gains nothing."""
    resolved_store = review_module.review_store_root()
    assert (
        fixture.config_home in resolved_store.parents
        or resolved_store.is_relative_to(fixture.config_home)
    )

    real_project_dir = Path.home() / ".config" / "reckon" / "crew" / "reviews" / PROJECT
    assert not real_project_dir.exists(), (
        f"a fixture write escaped to the operator's store at {real_project_dir}"
    )
