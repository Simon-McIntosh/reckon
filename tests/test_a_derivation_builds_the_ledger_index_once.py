"""One derivation builds the ledger's run index once and shares it with every reader.

A single :func:`reckon.crew.obligation_snapshot.fleet_state` reads the ledger
from more than one place: the held-worktree screen asks for the refreshed rows
through the checkout's own root, and the utilisation reader that attributes a
run's overrun asks for the same ledger through the default root. Both name one
ledger file, but the region's reuse cache keys on the root as it was spelled, so
the two reads missed each other and the index was decoded twice for one
derivation's answer.

This pins the fact that closes the gap: with the routes that name the same
ledger, one derivation counts exactly one index build, and the derivation's
answer is unchanged from the route that spells the root explicitly. The counts
are taken over one synthesised crew home, so no reader reaches the operator's
own crew home.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligation_snapshot, runs
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "ledger-index-fixture"
SESSION = "coordinator-fixture"
RUN_FILES = 5
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


def _run_record(run_id: str, worktree: Path | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "node": {"id": run_id, "plan": "fixture-plan"},
    }
    if worktree is not None:
        record["node"]["id"] = worktree.name
        record["worktree_retention"] = {"worktree": str(worktree)}
    return record


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised crew home whose ledger holds run files and a held tree."""
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
        ("commit", "-q", "-m", "test: seed the ledger-index fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = root / "docs" / "state" / PROJECT
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    # The config-home state directory is the registered checkout's state, as it
    # is in production, so the default ledger route and the checkout route name
    # one file and one derivation must build one index over it.
    (config_home / "state").mkdir()
    (config_home / "state" / PROJECT).symlink_to(ledger_dir, target_is_directory=True)
    # A registered worktree no live run occupies, so the held-worktree screen
    # runs; one run file names it, so the screen has a row to answer with.
    holder = tmp_path / "held-worktree"
    holder.mkdir()
    _git(root, "worktree", "add", "-q", "--detach", str(holder))
    for index in range(RUN_FILES):
        run_id = f"run-{index:02d}"
        record = _run_record(run_id, holder if index == 0 else None)
        runs._write_json(ledger_dir / "runs" / f"{run_id}.json", record)
    for index in range(RUN_FILES):
        run_id = f"run-{index:02d}"
        head = _git(root, "rev-parse", "HEAD")
        manifest = config_home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n",
            encoding="utf-8",
        )
        pointer: dict[str, Any] = {
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
        }
        if index == 0:
            # An overrun with a measured rate and a named backend and model is
            # what makes the utilisation reader reach the ledger for a
            # same-model reference rate.
            pointer.update(
                {
                    "backend": "fixture-backend",
                    "agent": {"model": "fixture-model"},
                    "throughput": {"tokens_per_second": 1.0},
                    "attempt_budget_seconds": 1,
                    "attempt_started_at": "2026-10-02T00:00:00+00:00",
                }
            )
        runs._write_json(runs.pointer_path(run_id), pointer)
    # The utilisation reader memoises its reference rate in-process, so a result
    # from another test's fixture must not answer for this one.
    obligations_module.recovery._historical_reference_rate.cache_clear()
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {"home": config_home, "repo": root, "ledger": ledger_dir, "holder": holder}


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


def _derive() -> obligation_snapshot.FleetState:
    # The utilisation reader memoises its reference rate for the process, so it
    # must be cleared for every derivation that needs it to reach the ledger.
    obligations_module.recovery._historical_reference_rate.cache_clear()
    return obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)


def test_one_derivation_builds_the_index_once(fleet: dict[str, Path]) -> None:
    """One fleet_state builds the ledger's run index once, not once per route."""
    with _CallCounter(ledger_module, "_index_payload") as index:
        state = _derive()
    # The check sees something: this fixture's held tree is named by a run file
    # and its overrun pointer reaches the utilisation reader, so both ledger
    # readers run and a zero here would be the instrument, not the code.
    assert state.held_worktrees, (
        "no held tree was reported, so the count proves nothing"
    )
    assert index.calls == 1, f"one derivation built the index {index.calls} times"


def test_the_shared_index_answers_as_the_explicit_root(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sharing the index leaves every answer as the explicit-root route gives it."""
    shared = _derive()
    monkeypatch.setattr(
        obligations_module, "_ledger_read_root", lambda _project, repository: repository
    )
    explicit = _derive()
    # The held-worktree duty is the answer the ledger read produces: it is the
    # row set the held screen read, and it must be the same for both routes.
    assert shared.held_worktrees, "no held tree was reported, so nothing was compared"
    assert explicit.held_worktrees == shared.held_worktrees
    # The explicit-root route is the one that needs a second build, so a count
    # of one above cannot be the instrument reporting the old behaviour.
    with _CallCounter(ledger_module, "_index_payload") as index:
        _derive()
    assert index.calls == 2, (
        f"the explicit-root route built the index {index.calls} times, so the"
        " shared route's single build is not distinguished from it"
    )


def test_a_changed_run_file_is_reflected_in_the_next_derivation(
    fleet: dict[str, Path],
) -> None:
    """A run file changed between two derivations is read afresh, not cached."""
    before = _derive().held_worktrees
    assert any(
        duty["run_id"] == "run-00" for duties in before.values() for duty in duties
    ), f"the first derivation did not report the held tree: {before}"
    path = fleet["ledger"] / "runs" / "run-00.json"
    changed = _run_record("run-00", None)
    runs._write_json(path, changed)
    after = _derive().held_worktrees
    assert before != after, "the edited run file was not reflected in the next answer"
    assert not any(
        duty["run_id"] == "run-00" for duties in after.values() for duty in duties
    ), f"the edited run file was still reported as holding the tree: {after}"
