"""A sweep resolves each live run's reviewed head and the ledger index once.

The producer derives the fleet about once a minute in one long-lived process.
Before this change every reader inside that derivation asked the same two
questions of the same live run again: the reviewed head and tree were resolved
once per reader through ``recovery._review_head_and_tree``, and the ledger's run
index was rebuilt for each ledger read. On the head profile that was the
review-resolution cluster, about 5 s, and the ledger scan, about 5 s more.

Two regions now bound those: one open around a whole sweep that resolves each
live run's reviewed head and tree once, and one that builds the run index once
for every ledger read inside it. The slices a sweep builds afterwards join the
same regions, so a session's payload reads the heads the derivation resolved.
This pins the facts that makes true: one derivation builds the index once and
resolves each live run's head at most once, one sweep does the same across its
derivation and every session's slice, and the obligations the snapshot lists
are the ones the derivation lists over the same state.

The counts are taken over one synthesised crew state under a temporary
configuration home, so no reader reaches the operator's own crew home.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
dispatch_module = importlib.import_module("reckon.crew.recovery_review_dispatch")

PROJECT = "review-head-fixture"
SESSION = "coordinator-fixture"
LIVE_RUNS = 6
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project of live runs that each owe a review of their head."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir()
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the review-head fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = root / "docs" / "state" / PROJECT
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    for index in range(LIVE_RUNS):
        run_id = f"run-{index:02d}"
        reviewed_head = _git(root, "rev-parse", "HEAD")
        head = _commit(root, f"work-{index:02d}.txt", f"work {index}\n")
        manifest = config_home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
            encoding="utf-8",
        )
        runs._write_json(
            runs.pointer_path(run_id),
            {
                "run_id": run_id,
                "project": PROJECT,
                "session": SESSION,
                "repo": str(root),
                "worktree": str(root),
                "base_sha": head,
                "process_alive": False,
                "launch": "in-harness",
                "role": "implement",
                "manifest_path": str(manifest),
                "node": {
                    "id": run_id,
                    "plan": "fixture-plan",
                    "section": "fixture-section",
                    "write_paths": ["seed.txt"],
                },
            },
        )
        # The stored review names the revision before the run's newest commit, so
        # the run reads review-missing and the head it carries must be resolved.
        emitted = "\n".join(
            f"SCORE {dimension}: 20" for dimension in review_module.REVIEW_DIMENSIONS
        )
        record = review_module.parse_review(emitted)
        record.update(
            {
                "project": PROJECT,
                "reviewed_run_id": run_id,
                "review_run_id": f"review-of-{run_id}",
                "reviewed_base_sha": reviewed_head,
                "reviewed_head_sha": reviewed_head,
            }
        )
        review_module.store_review(record)
    # A registered worktree no ledger record names: the held-worktree scan falls
    # back to the whole-ledger read, which is what builds the run index here.
    holder = tmp_path / "held-worktree"
    holder.mkdir()
    _git(root, "worktree", "add", "-q", "--detach", str(holder))
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {"home": config_home, "repo": root, "ledger": ledger_dir}


class _CallCounter:
    """Count calls to one function, without changing what it returns."""

    def __init__(self, module: Any, name: str) -> None:
        self._module = module
        self._name = name
        self.calls = 0

    def __enter__(self) -> Self:
        self._real = getattr(self._module, self._name)
        counter = self

        def counting(*args: Any, **kwargs: Any) -> Any:
            counter.calls += 1
            return counter._real(*args, **kwargs)

        setattr(self._module, self._name, counting)
        return self

    def __exit__(self, *exc: object) -> None:
        setattr(self._module, self._name, self._real)


def _derive(fleet: dict[str, Path]) -> None:
    obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)


def _sweep(fleet: dict[str, Path]) -> None:
    obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer={
            "pid": 1,
            "pid_start_time": "1",
            "started_at": None,
            "code_stamp": "review-head-fixture",
        },
        stream_offset=0,
        transition_fired=True,
        state_dirs=[],
        now=OBSERVED_AT,
    )


def test_one_derivation_builds_the_index_once(fleet: dict[str, Path]) -> None:
    """One fleet_state builds the ledger's run index once, not once per read."""
    with _CallCounter(ledger_module, "_index_payload") as index:
        _derive(fleet)
    # The check sees something: this fixture's held-worktree fallback does read
    # the ledger, so a zero here would be the instrument, not the code.
    assert index.calls == 1, f"one derivation built the index {index.calls} times"


def test_one_derivation_resolves_each_reviewed_head_once(
    fleet: dict[str, Path],
) -> None:
    """One fleet_state resolves each live run's reviewed head at most once."""
    live = len(runs.list_live(project=PROJECT))
    assert live >= LIVE_RUNS
    with _CallCounter(dispatch_module, "_reviewed_run_head") as heads:
        _derive(fleet)
    assert heads.calls >= live, (
        f"the fixture resolved no reviewed head over {live} live runs, so the"
        " count below proves nothing"
    )
    assert heads.calls <= live, (
        f"one derivation resolved {heads.calls} reviewed heads over {live} runs"
    )


def test_one_sweep_resolves_each_reviewed_head_once(fleet: dict[str, Path]) -> None:
    """A whole sweep, derivation and slices, resolves each head at most once."""
    live = len(runs.list_live(project=PROJECT))
    with _CallCounter(dispatch_module, "_reviewed_run_head") as heads:
        _sweep(fleet)
    assert heads.calls <= live, (
        f"one sweep resolved {heads.calls} reviewed heads over {live} runs, so a"
        " slice reached the head the derivation had already resolved"
    )


def _without_age(item: dict[str, Any]) -> dict[str, Any]:
    """One duty with its age dropped, which a snapshot stores as an instant."""
    return {
        key: value
        for key, value in item.items()
        if key not in {"age_seconds", "age_since", "until"}
    }


def test_snapshot_obligations_match_the_derivation(fleet: dict[str, Path]) -> None:
    """The snapshot lists the obligations the derivation lists over same state."""
    _sweep(fleet)
    snapshot = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert snapshot is not None
    reference = obligations_module.obligations(PROJECT, SESSION)
    assert [_without_age(item) for item in snapshot["obligations"]] == [
        _without_age(item) for item in reference["obligations"]
    ], (
        "the snapshot's obligations differ from the derivation's:"
        f" snapshot={snapshot['obligations']} reference={reference['obligations']}"
    )
