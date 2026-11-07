"""A steady-state publish recomputes only the runs whose inputs moved.

The station derives every live session's obligations on each tick. Rebuilding a
run's review tier runs git and reads its plan; the held-worktree scan reads the
merged ledger and git's registry; the classifier rewrites each run's memo. None
of those answers changes between two ticks while the run's head, manifest,
plan, the ledger and the live pointer directory all hold still, so a second
tick should read none of them again.

The fixture is a synthesised crew home of six live runs, each in its own
worktree and each owing a review of its head, so every one of those paths is
reached on the first tick. Counts are taken through patches over the second
tick, and every payload is compared with the one the same state produces with
the memos cleared.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import pytest

from reckon.crew import obligation_snapshot, runs
from reckon.crew import review as review_module
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_module = importlib.import_module("reckon.crew.recovery")
promotion_checks_module = importlib.import_module("reckon.crew.promotion_checks")
store_module = importlib.import_module("reckon._store")

PROJECT = "recompute-fixture"
SESSION = "coordinator-fixture"
LIVE_RUNS = 6
OBSERVED_AT = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
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
        ("commit", "-q", "-m", "test: seed the recompute fixture"),
    ):
        _git(root, *arguments)

    # A plan document the review tier reads its declared risk from, so the
    # tier's plan input is a real file whose stat identity keys the memo.
    (root / "docs" / "plans" / "fixture-plan.html").write_text(
        '<html><head><meta name="plan-version" content="1"></head><body>'
        '<section data-reckon="section" data-id="fixture-section" '
        'data-capability-risk="light"></section></body></html>',
        encoding="utf-8",
    )

    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = root / "docs" / "state" / PROJECT
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    (ledger_dir / "crew.json").write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )

    worktrees: dict[str, Path] = {}
    for index in range(LIVE_RUNS):
        run_id = f"run-{index:02d}"
        reviewed_head = _git(root, "rev-parse", "HEAD")
        worktree = tmp_path / f"worktree-{index:02d}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), reviewed_head)
        head = _commit(worktree, f"work-{index:02d}.txt", f"work {index}\n")
        worktrees[run_id] = worktree
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
                "worktree": str(worktree),
                "base_sha": reviewed_head,
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
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    return {
        "home": config_home,
        "repo": root,
        "ledger": ledger_dir,
        "worktrees": worktrees,
    }


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


class _GitCounter:
    """Count git subprocesses, whether they run through any helper."""

    def __init__(self) -> None:
        self.calls = 0

    def __enter__(self) -> Self:
        self._real = subprocess.run
        counter = self

        def counting(command: Any, *args: Any, **kwargs: Any) -> Any:
            if command and str(command[0]).endswith("git"):
                counter.calls += 1
            return counter._real(command, *args, **kwargs)

        subprocess.run = counting
        return self

    def __exit__(self, *exc: object) -> None:
        subprocess.run = self._real


class _MemoWriteCounter:
    """Count classification-memo writes, which land through the store's rename."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __enter__(self) -> Self:
        self._real = store_module.write_atomically
        counter = self

        def counting(path: Any, *args: Any, **kwargs: Any) -> Any:
            counter.paths.append(str(path))
            return counter._real(path, *args, **kwargs)

        store_module.write_atomically = counting
        return self

    def __exit__(self, *exc: object) -> None:
        store_module.write_atomically = self._real

    def memo_writes(self) -> list[str]:
        return [path for path in self.paths if path.endswith("classification.json")]


def _tick() -> tuple[Any, dict[str, Any]]:
    """One station tick: derive the fleet state, then slice one payload."""
    state = obligation_snapshot.fleet_state(PROJECT, now=OBSERVED_AT)
    return state, obligation_snapshot.payload_for(state, SESSION)


def _clear_memos() -> None:
    """Drop the memos when the module carries them.

    The base revision has no memo store, so the same test can run against it and
    fail on the second tick's counts rather than on a missing attribute.
    """
    clear = getattr(obligations_module, "_clear_cached_obligation_inputs", None)
    if clear is not None:
        clear()


def _assert_fixture_reaches_the_tier() -> None:
    """Guard the instrument: this fixture must owe reviews and resolve tiers.

    A fixture that classified its runs as anything else would leave the review
    tier untouched, and the second tick's zero would then measure nothing.
    """
    rows = obligations_module._classified_rows(PROJECT)
    assert len(rows) >= LIVE_RUNS, f"the fixture classified only {len(rows)} runs"
    kinds = {str(row.get("classification") or "") for row in rows}
    assert kinds & obligations_module.REVIEW_CLASSIFICATION_KINDS, (
        f"no run owes a review, so the tier path is never reached: {kinds}"
    )


def test_a_steady_tick_recomputes_nothing(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second tick with every input unmoved reads none of them again."""
    _assert_fixture_reaches_the_tier()
    _clear_memos()
    # The first tick warms every memo and writes each stale classification memo.
    _tick()

    with (
        _CallCounter(promotion_checks_module, "_plan_state_for_run") as plans,
        _CallCounter(ledger_module, "runs") as ledger_reads,
        _GitCounter() as git_calls,
        _MemoWriteCounter() as memo_writes,
    ):
        _state, payload = _tick()

    assert plans.calls == 0, (
        f"the second tick read plan state {plans.calls} times with nothing moved"
    )
    assert ledger_reads.calls == 0, (
        f"the second tick read the ledger {ledger_reads.calls} times with nothing moved"
    )
    assert git_calls.calls == 0, (
        f"the second tick ran {git_calls.calls} git subprocesses with nothing moved"
    )
    assert memo_writes.memo_writes() == [], (
        f"the second tick rewrote classification memos: {memo_writes.memo_writes()}"
    )
    assert payload["obligations"], "the payload the second tick built is empty"


def test_moving_one_head_recomputes_only_that_run(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One run's head moving recomputes that run's tier and nobody else's."""
    _assert_fixture_reaches_the_tier()
    _clear_memos()
    _tick()

    moved = "run-02"
    worktree = fleet["worktrees"][moved]
    _commit(worktree, "moved.txt", "the head moved\n")

    recomputed: list[str] = []
    real = recovery_module._sweep_review_tier

    def recording(record: Any, manifest_commits: Any) -> Any:
        recomputed.append(str(record.get("run_id") or ""))
        return real(record, manifest_commits)

    monkeypatch.setattr(recovery_module, "_sweep_review_tier", recording)
    _tick()
    assert recomputed == [moved], (
        "moving one run's head recomputed tiers for: "
        f"{recomputed} (expected only {moved})"
    )


def _without_age(payload: dict[str, Any]) -> dict[str, Any]:
    """A payload with its wall-clock ages dropped.

    Ages are read from the process clock between the two ticks, so they move by
    a second whatever the memos do; every fact a duty is derived from is what
    this comparison is about.
    """
    trimmed = {key: value for key, value in payload.items() if key != "summary"}
    trimmed["summary"] = {
        key: value
        for key, value in (payload.get("summary") or {}).items()
        if key != "oldest_age_seconds"
    }
    trimmed["obligations"] = [
        {
            key: value
            for key, value in item.items()
            if key not in {"age_seconds", "age_since", "until"}
        }
        for item in payload.get("obligations") or []
    ]
    return trimmed


def test_a_cleared_memo_yields_the_same_payload(fleet: dict[str, Any]) -> None:
    """Every payload equals the one the same state produces with memos cleared."""
    _assert_fixture_reaches_the_tier()
    _clear_memos()
    _tick()
    cached_state, cached = _tick()
    _clear_memos()
    fresh_state, fresh = _tick()
    assert _without_age(cached) == _without_age(fresh), (
        "the memoised payload differs from the recomputed one"
    )
    assert _without_age(cached) == _without_age(
        obligation_snapshot.payload_for(fresh_state, SESSION)
    )
    assert fresh_state.rows and cached_state.rows
