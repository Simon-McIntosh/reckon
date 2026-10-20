"""A review record must state a claim before either store path accepts it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew import review
from reckon.crew.runs import _write_json, crew_home, pointer_path

PROJECT = "claimless-review-fixture"
REVIEWED_RUN = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN = "r-20260101T010000000000-review-of-the-run"
CLAIMLESS = {
    "message": "the result cannot be reproduced",
    "path": "module.py:10",
    "severity": "follow-on",
}


def _record(finding: dict[str, str]) -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "scores": {"goal_fidelity": 12},
        "findings": [finding],
    }


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


@pytest.fixture()
def review_run(isolated_reckon_home: Path, tmp_path: Path) -> tuple[Path, Path]:
    assert crew_home().is_relative_to(isolated_reckon_home)
    assert review.review_store_root().is_relative_to(isolated_reckon_home)
    record_path = review.review_path(PROJECT, REVIEWED_RUN)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    repository = tmp_path / "repo"
    repository.mkdir()
    manifest = tmp_path / "manifest.md"
    manifest.write_text(
        f"node: review-of-the-run\nstatus: complete\ncommits: none\n"
        f"changed_paths:\n  - {record_path}\n"
        "tests: record parsed and checked\n",
        encoding="utf-8",
    )
    _write_json(
        pointer_path(REVIEW_RUN),
        {
            "run_id": REVIEW_RUN,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": "0" * 40,
            "launch": "cli",
            "role": "review",
            "manifest_path": str(manifest),
            "node": {
                "id": "review-of-the-run",
                "plan": "fixture",
                "section": "s1",
                "role": "review",
                "write_paths": [str(record_path)],
            },
        },
    )
    return record_path, manifest


def _check() -> tuple[int, list[str]]:
    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", REVIEW_RUN]
    )
    return result.exit_code, json.loads(result.output)["findings"]


def _assert_claim_refusal(message: str) -> None:
    assert "index 0" in message
    for key in (*CLAIMLESS, *review.FINDING_CLAIM_FIELDS):
        assert key in message


def test_claimless_entries_keep_their_positions() -> None:
    entries = review.claimless_findings(
        {"findings": [{"text": "one claim"}, CLAIMLESS, {"title": "another"}, None]}
    )
    assert entries == [(1, CLAIMLESS), (3, None)]


def test_store_refuses_a_claimless_record_without_writing(
    review_run: tuple[Path, Path], tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="index 0") as caught:
        review.store_review(_record(CLAIMLESS), base_dir=tmp_path / "store")
    _assert_claim_refusal(str(caught.value))
    assert _files(tmp_path / "store") == []


def test_check_manifest_refuses_a_handwritten_claimless_record(
    review_run: tuple[Path, Path],
) -> None:
    record_path, _manifest = review_run
    record_path.write_text(json.dumps(_record(CLAIMLESS)), encoding="utf-8")
    exit_code, findings = _check()
    assert exit_code != 0
    assert len(findings) == 1
    assert str(record_path) in findings[0]
    _assert_claim_refusal(findings[0])


def test_a_stated_claim_passes_both_paths(review_run: tuple[Path, Path]) -> None:
    record_path, _manifest = review_run
    finding = {
        "text": CLAIMLESS["message"],
        "path": "module.py:10",
        "severity": "follow-on",
    }
    assert review.store_review(_record(finding)) == record_path
    exit_code, findings = _check()
    assert exit_code == 0, findings
    assert findings == []
