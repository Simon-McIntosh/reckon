"""A refused promotion request settles once its run has landed or gone.

A durable refusal is a duty while the run still needs the coordinator, and
nothing else afterwards: refused requests kept raising promotion-failed rows
for runs that had since been promoted by hand or discarded, with a next command
that could no longer apply. The behaviours asserted here are:

- a refusal whose run is then promoted in the caller's process raises no duty,
  and the request is recorded as settled with its refusal kept;
- the same for a refusal whose run is discarded;
- ``crew ack`` records a deferral for a run with no live pointer, so a row that
  cannot be settled is still deliberately deferrable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import _plan_html
from reckon.cli import main
from reckon.crew import obligations, promotion, runs
from reckon.crew import promotion_requests as requests
from tests.helpers.git import git

PROJECT = "refused-promotion-fixture"
SESSION = "refused-promotion-session"
PLAN = "refused-promotion-target"
REVIEWED_RUN_ID = "r-20261114T090000000001-reviewed-run"
REVIEW_RUN_ID = "r-20261114T090100000000-review-of-reviewed-run"
REVIEW_NODE_ID = f"review-of-{REVIEWED_RUN_ID}"
REVIEWED_BASE = "b" * 40
REVIEWED_HEAD = "a" * 40
REFUSAL = "the suite arm failed"


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


def _store_review_record() -> Path:
    from reckon.crew import review as review_module

    record = {
        "status": "parsed",
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN_ID,
        "review_run_id": REVIEW_RUN_ID,
        "reviewed_base_sha": REVIEWED_BASE,
        "reviewed_head_sha": REVIEWED_HEAD,
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 20),
        "total": 20 * len(review_module.REVIEW_DIMENSIONS),
        "findings": [],
        "timestamp": "2026-11-14T08:01:00Z",
    }
    path = review_module.review_path(
        PROJECT, REVIEWED_RUN_ID, reviewed_head_sha=REVIEWED_HEAD
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


@pytest.fixture()
def repository(isolated_reckon_home: Path, tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_resource(
        root / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Refused Promotion Target",
            "status": None,
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
        git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(root, "add", "seed.txt", "docs")
    git(root, "commit", "-q", "-m", "chore: seed repository")
    (isolated_reckon_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _plant_review_run(repository: Path, tmp_path: Path) -> None:
    """A completed review run whose stored record is on disk and worker exited."""
    _store_review_record()
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "node: review-of-reviewed-run\n"
        "status: complete\n"
        "checkpoint: the review record is stored\n",
        encoding="utf-8",
    )
    pointer = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "session": SESSION,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": "review",
        "backend": "native",
        "phase": "complete",
        "created_at": "2026-11-14T08:01:00Z",
        "manifest_path": str(manifest),
        "node": {
            "id": REVIEW_NODE_ID,
            "plan": PLAN,
            "section": "s52",
            "time_budget": "25m",
            "role": "review",
            "write_paths": [str(_store_review_record())],
        },
    }
    runs._write_json(runs.pointer_path(REVIEW_RUN_ID), pointer)
    directory = Path(runs.run_dir(REVIEW_RUN_ID))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "exit.json").write_text(
        json.dumps({"run_id": REVIEW_RUN_ID, "exit_code": 0}), encoding="utf-8"
    )


def _refuse(run_id: str) -> None:
    """Leave the durable state a refused drain leaves: the request kept in ERRORS."""
    requested = requests.request_promotion(run_id, options={"gate": "not-run"})
    requests._finish(requested, requests.REFUSED, ok=False, error=REFUSAL)


def _refused_run_ids() -> list[str]:
    return [item["run_id"] for item in requests.failure_obligations(PROJECT, SESSION)]


def test_a_promoted_run_settles_its_refused_request(repository, tmp_path):
    _plant_review_run(repository, tmp_path)
    _refuse(REVIEW_RUN_ID)
    assert _refused_run_ids() == [REVIEW_RUN_ID], "the refusal must raise a duty first"

    result = promotion.complete(
        REVIEW_RUN_ID, gate="not-run", root=repository, promoted_by="review-self"
    )
    assert result["run_id"] == REVIEW_RUN_ID

    assert _refused_run_ids() == []
    settled = requests.read_promotion(REVIEW_RUN_ID)
    assert settled["phase"] == requests.SETTLED, settled
    assert settled["settled"] == "promoted", settled
    assert settled["error"] == REFUSAL, "the record of the refusal is kept"
    assert requests.PromotionService().tick() == []
    print(f"promoted {REVIEW_RUN_ID}: request settled as {settled['settled']}")


def test_a_discarded_run_settles_its_refused_request(repository, tmp_path):
    _plant_review_run(repository, tmp_path)
    _refuse(REVIEW_RUN_ID)
    assert _refused_run_ids() == [REVIEW_RUN_ID]

    promotion.discard(REVIEW_RUN_ID)
    assert not runs.pointer_path(REVIEW_RUN_ID).exists()

    assert _refused_run_ids() == []
    settled = requests.read_promotion(REVIEW_RUN_ID)
    assert settled["phase"] == requests.SETTLED, settled
    assert settled["settled"] == "discarded", settled
    assert settled["error"] == REFUSAL, "the record of the refusal is kept"
    print(f"discarded {REVIEW_RUN_ID}: request settled as {settled['settled']}")


def test_a_landed_run_settles_its_refused_request_as_landed(repository, tmp_path):
    """A ledger row outranks the missing pointer: the run landed, it did not vanish."""
    from reckon import ledger

    _plant_review_run(repository, tmp_path)
    _refuse(REVIEW_RUN_ID)
    assert _refused_run_ids() == [REVIEW_RUN_ID]

    row = {"run_id": REVIEW_RUN_ID, "commits": ["a" * 40]}
    ledger.run_path(PROJECT, REVIEW_RUN_ID, repository).parent.mkdir(
        parents=True, exist_ok=True
    )
    ledger.write_run_file(PROJECT, row, root=repository)
    assert ledger.run_path(PROJECT, REVIEW_RUN_ID, repository).is_file()
    runs.pointer_path(REVIEW_RUN_ID).unlink()

    assert _refused_run_ids() == []
    settled = requests.read_promotion(REVIEW_RUN_ID)
    assert settled["phase"] == requests.SETTLED, settled
    assert settled["settled"] == "landed", settled
    assert settled["error"] == REFUSAL, "the record of the refusal is kept"
    print(f"landed {REVIEW_RUN_ID}: request settled as {settled['settled']}")


def test_a_run_with_no_pointer_and_no_record_settles_its_refusal_as_gone(
    repository, tmp_path
):
    from reckon import ledger

    _plant_review_run(repository, tmp_path)
    _refuse(REVIEW_RUN_ID)
    assert _refused_run_ids() == [REVIEW_RUN_ID]

    runs.pointer_path(REVIEW_RUN_ID).unlink()
    assert not promotion.discard_recorded(REVIEW_RUN_ID)
    assert not ledger.run_path(PROJECT, REVIEW_RUN_ID, repository).exists()

    assert _refused_run_ids() == []
    settled = requests.read_promotion(REVIEW_RUN_ID)
    assert settled["phase"] == requests.SETTLED, settled
    assert settled["settled"] == "gone", settled
    assert settled["error"] == REFUSAL, "the record of the refusal is kept"
    assert requests.PromotionService().tick() == []
    print(f"gone {REVIEW_RUN_ID}: request settled as {settled['settled']}")


def _duty_row(run_id: str) -> dict:
    return {
        "kind": "promotion-failed",
        "run_id": run_id,
        "node": "gone-run",
        "plan": PLAN,
        "age_seconds": 60,
        "reason": REFUSAL,
        "next_command": f"reckon crew complete --run {run_id} --gate passed",
    }


def test_ack_defers_a_row_for_a_run_with_no_live_pointer(repository, monkeypatch):
    run_id = "r-20261114T091500000000-gone-run"
    assert not runs.pointer_path(run_id).exists()

    acknowledged = CliRunner().invoke(
        main,
        [
            "crew",
            "ack",
            "--run",
            run_id,
            "--project",
            PROJECT,
            "--until",
            "2031-01-01T00:00:00+00:00",
            "--reason",
            "the run is gone and its refusal no longer applies",
        ],
    )
    assert acknowledged.exit_code == 0, acknowledged.output

    acks = obligations._acknowledgements_in_force(PROJECT, now=datetime.now(UTC))
    assert run_id in acks, acks

    monkeypatch.setattr(
        requests, "failure_obligations", lambda *a, **k: [_duty_row(run_id)]
    )
    view = obligations.obligations(PROJECT, SESSION)
    assert [item["run_id"] for item in view["obligations"]] == [], view
    assert [item["run_id"] for item in view["acknowledged"]] == [run_id], view
    print("a run with no live pointer was acknowledged and reads deferred")
