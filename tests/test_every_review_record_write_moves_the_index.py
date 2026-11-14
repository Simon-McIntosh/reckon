"""Every review writer publishes its run's digest without reader reconciliation."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon._store import write_json_atomically
from reckon.cli import main
from reckon.crew import plan_review, review, review_records, runs

PROJECT = "writer-fixture"
RUN = "r-20261114T090000000000-reviewed"
REVIEW_RUN = "r-20261114T100000000000-review"
WRITERS = (
    "store_review",
    "store_committed_review",
    "commit_record_write",
    "repair",
    "dispose_finding",
    "dispose_dimension",
    "store_code_review",
    "store_plan_review",
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def world(tmp_path, isolated_reckon_home):
    repository = tmp_path / "repository"
    docs = repository / "docs"
    docs.mkdir(parents=True)
    seed = repository / "seed.txt"
    seed.write_text("review subject\n")
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "add", "seed.txt")
    _git(
        repository,
        "commit",
        "-q",
        "-m",
        "test: seed a review subject\n\nGive the review a resolvable revision.",
    )
    head = _git(repository, "rev-parse", "HEAD")
    (isolated_reckon_home / "mounts.json").write_text(json.dumps({PROJECT: str(docs)}))
    return {
        "project": PROJECT,
        "reviewed_run_id": RUN,
        "review_run_id": REVIEW_RUN,
        "reviewed_base_sha": head,
        "reviewed_head_sha": head,
        "status": "parsed",
        "added_failure_count": 0,
        "added_failure_ids": [],
        "timestamp": "2026-11-14T10:00:00+00:00",
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 18),
        "findings": [{"file": "seed.txt", "line": "1", "text": "Check the subject."}],
    }


def _prepare(writer, body):
    """Seed the record without using any writer whose publication is measured."""
    body = dict(body)
    if writer == "store_committed_review":
        # The explicit committed_root argument accepts any directory. Keeping
        # this fixture beneath the staging root lets the same index measure
        # the committed writer without changing its routing or index policy.
        committed = review.review_store_root() / PROJECT / "committed"
        path = review.review_path(
            PROJECT, RUN, committed_root=committed, review_run_id=REVIEW_RUN
        )
    elif writer == "store_code_review":
        body["neighbourhood_key"] = "neighbourhood"
        path = plan_review.code_review_path(PROJECT, body["neighbourhood_key"])
    elif writer == "store_plan_review":
        body.update(plan_slug="subject", plan_version=1)
        path = plan_review.plan_review_path(
            PROJECT, "subject", 1, review_run_id=REVIEW_RUN
        )
    else:
        path = review.review_path(
            PROJECT, RUN, reviewed_head_sha=body["reviewed_head_sha"]
        )
    write_json_atomically(path, body, create_parents=True)
    if writer == "repair":
        source = review.run_review_record_directory(runs.run_dir(RUN)) / f"{RUN}.json"
        write_json_atomically(source, body, create_parents=True)
    review_records.store_index_document(path.parent)
    return path, body


def _invoke(writer, path, body):
    changed = {**body, "timestamp": "2026-11-14T11:00:00+00:00"}
    if writer == "store_review":
        review.store_review(changed)
    elif writer == "store_committed_review":
        review.store_committed_review(changed, committed_root=path.parents[2])
    elif writer == "commit_record_write":
        # This helper accepts writes made by callers. Keep the inode and the
        # directory identity fixed, so only publication can move the index.
        path.write_text(json.dumps(changed))
        review_records.commit_record_write(PROJECT, path)
    elif writer == "repair":
        source = review.run_review_record_directory(runs.run_dir(RUN)) / f"{RUN}.json"
        result = CliRunner().invoke(
            main,
            [
                "crew",
                "repair",
                "--project",
                PROJECT,
                "--review",
                "--run",
                RUN,
                "--record",
                str(source),
                "--set",
                "status=unparsed",
                "--reason",
                "The record needs a corrected status.",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["filed"] == str(path)
    elif writer.startswith("dispose_"):
        argument = (
            ["--finding", "0"]
            if writer == "dispose_finding"
            else ["--dimension", "goal_fidelity"]
        )
        result = CliRunner().invoke(
            main,
            [
                "crew",
                "dispose",
                "--project",
                PROJECT,
                "--run",
                RUN,
                "--review",
                REVIEW_RUN,
                *argument,
                "--kind",
                "exempted",
                "--reason",
                "The fixture accepts this finding.",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["path"] == str(path)
    elif writer == "store_code_review":
        plan_review.store_code_review(changed)
    elif writer == "store_plan_review":
        plan_review.store_plan_review(changed)
    else:
        raise AssertionError(writer)


def _measure(writer, body, monkeypatch, *, disabled=False):
    path, body = _prepare(writer, body)
    directory = path.parent
    before_digest = review_records.store_index_run_digest(directory, RUN)
    before_version = review_records.store_index_version(directory)
    assert before_digest, "the positive control must already be indexed"
    with monkeypatch.context() as patch:
        # A reader's fallback must not conceal a writer bypass. The foreign
        # write tests independently measure that fallback.
        patch.setattr(review_records, "reconcile_foreign_write", lambda *_: 0)
        if disabled:
            patch.setattr(review_records, "record_written", lambda *_, **__: None)
            patch.setattr(review, "record_written", review_records.record_written)
        _invoke(writer, path, body)
        digest_changed = (
            review_records.store_index_run_digest(directory, RUN) != before_digest
        )
        version_advanced = (
            review_records.store_index_version(directory) > before_version
        )
    return digest_changed, version_advanced


@pytest.mark.parametrize("writer", WRITERS)
def test_every_writer_moves_the_index(writer, world, monkeypatch):
    digest_changed, version_advanced = _measure(writer, world, monkeypatch)
    assert digest_changed, f"{writer}: the indexed run digest did not change"
    assert version_advanced, f"{writer}: the store index version did not advance"


@pytest.mark.parametrize("writer", WRITERS)
def test_every_writer_check_detects_disabled_publication(writer, world, monkeypatch):
    digest_changed, version_advanced = _measure(
        writer, world, monkeypatch, disabled=True
    )
    assert not digest_changed, f"{writer}: the digest check missed disabled publication"
    assert not version_advanced, (
        f"{writer}: the version check missed disabled publication"
    )
