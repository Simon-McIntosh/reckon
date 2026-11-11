"""A mistyped review run id is refused at write time and can be repaired."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main
from reckon.crew import review, runs
from reckon.crew.recovery_review_delivery import review_delivery_refusal

PROJECT = "mistyped-review-fixture"
REVIEWED_RUN = "r-reviewed-fixture"
HEAD = "b" * 40
REVIEW_RUN = "r-20261110T233217626304-review-of-ripple-relations-948c28230595"
TRUNCATED = "r-20261110T233217626304-review-of-ripple-relations"


@pytest.fixture()
def record_path(isolated_reckon_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A review record stored in the reviewed run's own review-records tree."""
    assert runs.crew_home().is_relative_to(isolated_reckon_home)
    monkeypatch.setattr(review, "unresolved_reviewed_revision", lambda _body: None)
    declared = review.run_review_record_paths(
        runs.run_dir(REVIEWED_RUN), REVIEWED_RUN, reviewed_head_sha=HEAD
    )
    source = Path(declared[1])
    source.parent.mkdir(parents=True)
    runs._write_json(
        runs.pointer_path(REVIEWED_RUN),
        {"run_id": REVIEWED_RUN, "project": PROJECT, "role": "implement"},
    )
    runs._write_json(
        runs.pointer_path(REVIEW_RUN),
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "role": "review",
            "repo": str(source.parent.parent),
            "worktree": str(source.parent.parent),
            "manifest_path": str(runs.run_dir(REVIEW_RUN) / "manifest.md"),
            "node": {
                "id": "review-of-fixture",
                "role": "review",
                "write_paths": [str(source)],
            },
        },
    )
    return source


def _record(**changes: object) -> dict:
    body = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": TRUNCATED,
        "reviewed_base_sha": "a" * 40,
        "reviewed_head_sha": HEAD,
        "scores": dict.fromkeys(review.REVIEW_DIMENSIONS, 17),
        "added_failure_count": 0,
        "added_failure_ids": [],
        "findings": [],
        "status": "parsed",
    }
    body.update(changes)
    return body


def _invoke(*args: str):
    return CliRunner().invoke(main, ["crew", *args])


def _write(source: Path, body: dict) -> None:
    source.write_text(json.dumps(body, indent=4), encoding="utf-8")


def test_check_review_refuses_a_record_naming_a_prefix_of_the_run_id(
    record_path: Path,
) -> None:
    _write(record_path, _record())
    result = _invoke("check", "--review", "--run", REVIEW_RUN)
    assert result.exit_code == 1, result.output
    findings = json.loads(result.output)["findings"]
    assert any(
        REVIEW_RUN in finding and "review_run_id" in finding for finding in findings
    )


def test_check_record_form_still_accepts_a_prefix_without_the_writing_run(
    record_path: Path,
) -> None:
    _write(record_path, _record())
    result = _invoke("check", "--review", "--record", str(record_path))
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["findings"] == []


def test_repair_sets_the_full_review_run_id_and_complete_accepts_it(
    record_path: Path,
) -> None:
    _write(record_path, _record())
    result = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEW_RUN,
        "--record",
        str(record_path),
        "--set",
        f"review_run_id={REVIEW_RUN}",
        "--reason",
        "the record dropped the head suffix of its own review run id",
    )
    assert result.exit_code == 0, result.output
    repaired = json.loads(record_path.read_text(encoding="utf-8"))
    assert repaired["review_run_id"] == REVIEW_RUN
    assert repaired["reviewed_run_id"] == REVIEWED_RUN
    assert repaired["scores"] == _record()["scores"]
    refusal = review_delivery_refusal(runs.read_pointer(REVIEW_RUN), PROJECT)
    assert (
        refusal is None or refusal["condition"] != "record names a different review run"
    )
    checked = _invoke("check", "--review", "--run", REVIEW_RUN)
    assert checked.exit_code == 0, checked.output


def test_repair_refuses_an_id_that_does_not_extend_the_stored_one(
    record_path: Path,
) -> None:
    _write(record_path, _record())
    before = record_path.read_bytes()
    result = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEW_RUN,
        "--record",
        str(record_path),
        "--set",
        "review_run_id=r-20261110T233217626304-some-other-review",
        "--reason",
        "not an extension of the stored id",
    )
    assert result.exit_code != 0, result.output
    assert record_path.read_bytes() == before


def test_repair_still_refuses_a_record_outside_the_reviewed_runs_directory(
    record_path: Path, tmp_path: Path
) -> None:
    stray = tmp_path / "stray-review.json"
    _write(stray, _record())
    result = _invoke(
        "repair",
        "--review",
        "--run",
        REVIEW_RUN,
        "--record",
        str(stray),
        "--set",
        f"review_run_id={REVIEW_RUN}",
        "--reason",
        "outside every review-records directory",
    )
    assert result.exit_code != 0, result.output
    assert "review-records directory" in result.output
