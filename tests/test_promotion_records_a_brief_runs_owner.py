"""A brief run's promotion records its brief digest and, if it changes a plan's
product, the plan it belongs to.

A brief run carries no plan section, so nothing in the ordinary landing path
joins a product change to the plan that owns it. An implement-role brief run
therefore lands only with one of two discharges: ``--plan-link`` naming the plan
whose product it changed, or ``--unplanned-reason`` saying why it changed no
plan. Each test here makes the guarded thing happen and reads the refusal or the
row, rather than resting on the suite being green.

Every test is hermetic: ``RECKON_HOME`` moves the transient crew directory into
a temp tree and the repository is a real but throwaway git repo, so no real
ledger or live pointer directory is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from reckon import crew
from reckon.crew.runs import _write_json, pointer_path

PROJECT = "proj"
BRIEF_SHA = "b" * 64

# The pointer records the promoting process as the run's worker, so a promotion
# of one of these fixtures is admitted only with the live-run waiver an operator
# would give. What each case asserts is the ledger row a promotion writes.
_PLUMBING_WAIVER = (
    "the pytest process stands in for the worker; this fixture exercises "
    "promotion plumbing"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
    ):
        _git(root, *arguments)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt", "docs")
    _git(root, "commit", "-q", "-m", "test: seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _write_pointer(
    repository: Path,
    run_id: str,
    *,
    role: str = "implement",
    brief: str = "/tmp/brief.md",
    brief_sha256: str = BRIEF_SHA,
    brief_path: str = "",
) -> None:
    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "worktree": str(repository),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "pid": os.getpid(),
        "created_at": "2026-09-30T08:00:00Z",
        "node": {
            "id": "node-a",
            "plan": "",
            "section": "",
            "brief": brief,
            "brief_sha256": brief_sha256,
            "brief_path": brief_path,
            "time_budget": "25m",
            "write_paths": [],
        },
    }
    _write_json(pointer_path(run_id), record)


def _promote(repository: Path, run_id: str, **kwargs):
    return crew.complete(
        run_id, root=repository, live_run_waiver=_PLUMBING_WAIVER, **kwargs
    )


def _ledger_row(repository: Path, run_id: str) -> dict:
    rows = crew.ledger.runs(PROJECT, root=repository)
    return next(row for row in rows if row.get("run_id") == run_id)


def test_an_investigate_brief_run_promotes_with_no_extra_flag(
    repository: Path,
) -> None:
    run_id = "r-20260930T080000000000-node-a"
    _write_pointer(repository, run_id, role="investigate")

    promoted = _promote(repository, run_id, gate="passed", outcome="the probe ran")

    row = _ledger_row(repository, run_id)
    assert row["plan"] is None
    assert row["brief"]["sha256"] == BRIEF_SHA
    assert row["impl_move"]["verdict"] == "exempt"
    assert row["impl_move"]["reason"] == "brief-names-no-plan"
    assert row["plan_link"] is None and row["unplanned_reason"] is None
    assert promoted["run_id"] == run_id


def test_an_implement_brief_run_without_an_owner_is_refused(
    repository: Path,
) -> None:
    run_id = "r-20260930T080100000000-node-a"
    _write_pointer(repository, run_id, role="implement")

    with pytest.raises(crew.CrewError) as refusal:
        _promote(repository, run_id, gate="passed", outcome="code landed")

    message = str(refusal.value)
    assert "--plan-link" in message
    assert "--unplanned-reason" in message
    # The pointer survives a refusal, so the coordinator can retry it.
    assert pointer_path(run_id).exists()


def test_a_plan_link_lands_on_the_row(repository: Path) -> None:
    run_id = "r-20260930T080200000000-node-a"
    _write_pointer(repository, run_id, role="implement")

    _promote(
        repository,
        run_id,
        gate="passed",
        outcome="the section's product changed",
        plan_link="plan-a",
    )

    row = _ledger_row(repository, run_id)
    assert row["plan_link"] == "plan-a"
    assert row["unplanned_reason"] is None


def test_an_unplanned_reason_lands_on_the_row(repository: Path) -> None:
    run_id = "r-20260930T080300000000-node-a"
    _write_pointer(repository, run_id, role="implement")

    _promote(
        repository,
        run_id,
        gate="passed",
        outcome="a probe changed no plan",
        unplanned_reason="a measurement changes no plan's product",
    )

    row = _ledger_row(repository, run_id)
    assert row["unplanned_reason"] == "a measurement changes no plan's product"
    assert row["plan_link"] is None
