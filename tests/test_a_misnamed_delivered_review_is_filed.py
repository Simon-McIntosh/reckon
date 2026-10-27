"""A review record delivered under the wrong name is still filed.

A reviewer writes its record by hand, so it can name the file by its own run id
rather than the path its dispatch declared. Reading only the declared names
left such a record where no store reader found it, the reviewed run read as
unreviewed, and the coordinator filed it by hand. These cases make that
delivery happen and read the result:

* a well-formed record delivered under the review run's own id is filed by the
  review run's promotion and readable through ``crew check --review``;
* a record naming another run is left unfiled;
* a sibling review's record for the same reviewed run and head is not filed by
  this run's promotion, and the stored record and its dispositions survive;
* the dry-run receipt names the misnamed record a filing would open.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon.cli import main
from reckon.crew import promotion
from reckon.crew import review as review_module
from reckon.crew.runs import _write_json, crew_home, pointer_path, run_dir
from tests.helpers.git import git as _git

PROJECT = "misnamed-delivery-fixture"
PLAN = "misnamed-delivery-target"
REVIEWED_RUN_ID = "r-20261024T201437083115-reviewed-run"
REVIEW_RUN_ID = "r-20261024T201600000000-review-of-reviewed-run"
SIBLING_REVIEW_RUN_ID = "r-20261024T201700000000-review-of-reviewed-run"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40


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
    assert crew_home().is_relative_to(isolated_reckon_home)
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Misnamed Delivery Target",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (root / "docs" / "state" / PROJECT).mkdir(parents=True, exist_ok=True)
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
    return root


def _declared_paths() -> list[str]:
    """The record paths the review run's dispatch declared to it."""
    return review_module.run_review_record_paths(
        run_dir(REVIEWED_RUN_ID),
        REVIEWED_RUN_ID,
        reviewed_head_sha=REVIEWED_HEAD,
    )


def _record(**changes: object) -> dict:
    body = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": REVIEWED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
        "timestamp": "2026-10-24T21:00:00Z",
    }
    body.update(changes)
    return body


def _record_directory() -> Path:
    return run_dir(REVIEWED_RUN_ID) / review_module.RUN_REVIEW_RECORD_DIRNAME


def _deliver(body: dict, name: str) -> Path:
    """Write a record by hand into the reviewed run's record directory."""
    path = _record_directory() / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _build_review_run(tmp_path: Path, repository: Path) -> None:
    """A completed review run declaring the record paths its dispatch named."""
    # ``crew check --review`` resolves the reviewed run's project from its live
    # pointer. The pointer names no tree, so the carried revisions are not
    # resolved against a checkout this fixture does not build.
    _write_json(
        pointer_path(REVIEWED_RUN_ID),
        {"run_id": REVIEWED_RUN_ID, "project": PROJECT, "role": "implement"},
    )
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is written\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW_RUN_ID),
        {
            "run_id": REVIEW_RUN_ID,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "launch": "in-harness",
            "role": "review",
            "backend": "native",
            "created_at": "2026-10-24T21:00:00Z",
            "manifest_path": str(manifest),
            "node": {
                "id": f"review-of-{REVIEWED_RUN_ID}",
                "plan": PLAN,
                "section": "s25",
                "time_budget": "25m",
                "role": "review",
                "write_paths": _declared_paths(),
            },
        },
    )
    directory = Path(run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )


def _promote(repository: Path) -> dict:
    return promotion.complete(
        REVIEW_RUN_ID, gate="passed", root=repository, promoted_by="review-self"
    )


def _check_review() -> object:
    return CliRunner().invoke(
        main, ["crew", "check", "--review", "--run", REVIEWED_RUN_ID]
    )


def test_a_record_delivered_under_the_review_run_id_is_filed(
    repository: Path, tmp_path: Path
) -> None:
    """The measured shape: the file lands under the review run's own id."""
    _build_review_run(tmp_path, repository)
    misnamed = _deliver(_record(), REVIEW_RUN_ID)
    # The record is not where the run declared it, so nothing reads it yet.
    assert not review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    ).exists()
    assert _check_review().exit_code != 0

    _promote(repository)

    stored = review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    )
    assert stored.is_file()
    assert (
        json.loads(stored.read_text(encoding="utf-8"))["review_run_id"] == REVIEW_RUN_ID
    )
    check = _check_review()
    assert check.exit_code == 0, check.output
    assert misnamed.is_file()


@pytest.mark.parametrize("changed_run", ["reviewed_run_id", "review_run_id"])
def test_a_record_naming_another_run_is_left_unfiled(
    repository: Path, tmp_path: Path, changed_run: str
) -> None:
    """The content arm's selection needs both identities, not just one."""
    _build_review_run(tmp_path, repository)
    _deliver(
        _record(**{changed_run: "r-20261024T200000000000-another-run"}), REVIEW_RUN_ID
    )

    _promote(repository)

    assert not review_module.review_path(PROJECT, REVIEWED_RUN_ID).exists()
    assert _check_review().exit_code != 0


def test_a_sibling_reviews_record_is_not_filed_by_this_promotion(
    repository: Path, tmp_path: Path
) -> None:
    """A record another review delivered cannot displace the stored one."""
    _build_review_run(tmp_path, repository)
    # The stored record carries a disposition written after the review landed;
    # the copy the sibling delivered is the record as the sibling first wrote
    # it. Filing that copy would overwrite the disposition, which is what the
    # identity pair in the content arm prevents.
    stored = review_module.store_review(
        _record(
            review_run_id=SIBLING_REVIEW_RUN_ID,
            dimension_dispositions={"rigor": "folded"},
        )
    )
    before = stored.read_bytes()
    sibling = _deliver(
        _record(review_run_id=SIBLING_REVIEW_RUN_ID), SIBLING_REVIEW_RUN_ID
    )
    assert sibling.read_bytes() != before

    _promote(repository)

    assert sibling.is_file(), "the sibling's own record must be left where it is"
    assert stored.read_bytes() == before
    assert json.loads(stored.read_text(encoding="utf-8"))[
        review_module.DIMENSION_DISPOSITIONS_KEY
    ] == {"rigor": "folded"}


def test_the_misnamed_record_is_named_in_the_dry_run_receipt(
    repository: Path, tmp_path: Path
) -> None:
    """The receipt names the record the filing would open."""
    _build_review_run(tmp_path, repository)
    misnamed = _deliver(_record(), REVIEW_RUN_ID)

    report = promotion.complete_dry_run(REVIEW_RUN_ID, root=repository, gate="passed")

    assert str(misnamed) in report["would_file_reviews"]
    assert Path(report["would_file_reviews"][0]).parent == _record_directory()
    assert misnamed.is_file()
    assert not review_module.review_path(PROJECT, REVIEWED_RUN_ID).exists()
