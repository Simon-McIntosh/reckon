"""A review manifest states what its inbox delivered, or says it delivered none."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon.cli import main as cli_main
from reckon.crew.review import review_path
from reckon.crew.runs import _write_json, crew_home, pointer_path

PROJECT = "claimless-review-fixture"
REVIEWED_RUN = "r-20260101T000000000000-reviewed-run"
REVIEW_RUN = "r-20260101T010000000000-review-of-the-run"

MANIFEST = """\
node: review-of-the-run
status: complete
commits: none
changed_paths:
  - {record}
tests: record parsed and checked
{messages}"""

MESSAGES_FINDING = "messages_received: no delivery receipt was recorded"


def _record() -> dict:
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": REVIEW_RUN,
        "status": "parsed",
        "scores": {"goal_fidelity": 12},
        "findings": [{"text": "the result cannot be reproduced"}],
    }


@pytest.fixture()
def review_run(isolated_reckon_home: Path, tmp_path: Path) -> tuple[Path, Path]:
    assert crew_home().is_relative_to(isolated_reckon_home)
    record = review_path(PROJECT, REVIEWED_RUN)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(_record()), encoding="utf-8")
    repository = tmp_path / "repo"
    repository.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.md"
    manifest.write_text("", encoding="utf-8")
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
                "write_paths": [str(record)],
            },
        },
    )
    return record, manifest


def _check(manifest: Path, body: str) -> tuple[int, list[str]]:
    manifest.write_text(body, encoding="utf-8")
    result = CliRunner().invoke(
        cli_main, ["crew", "check", "--manifest", "--run", REVIEW_RUN]
    )
    return result.exit_code, json.loads(result.output)["findings"]


def _body(record: Path, messages: str) -> str:
    return MANIFEST.format(record=record, messages=messages)


def test_a_manifest_that_never_states_its_messages_is_refused(
    review_run: tuple[Path, Path],
) -> None:
    record, manifest = review_run
    exit_code, findings = _check(manifest, _body(record, ""))
    assert exit_code != 0
    assert any(MESSAGES_FINDING in finding for finding in findings)


def test_the_same_manifest_passes_once_it_states_no_messages(
    review_run: tuple[Path, Path],
) -> None:
    record, manifest = review_run
    body = _body(record, "messages_received: none\n")
    exit_code, findings = _check(manifest, body)
    assert exit_code == 0, findings
    assert findings == []
