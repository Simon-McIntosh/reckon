"""A station tick reads the ledger aggregate once, not once per delivered run.

The station evaluates every delivered run's review duty on every tick, and each
duty asks the ledger whether the run is closed. A run id set rebuilt from the
aggregate on every call makes one tick pay a full ledger parse per delivered
run. The ledger already keeps a stat-validated index of the aggregate, so the
membership read is served from it: the pass pays at most one aggregate parse per
project, and a later caller that asks the same question opens none at all.

The fixture is a synthesised crew home of delivered runs whose only stored
review describes an earlier head. Each therefore reads ``scoring`` with a
``review-missing`` duty the reflex owns, which is the branch that reaches
``_open_review_refusal`` and, through it, the ledger membership read. The
count is taken after one warm-up read, so the index build on a fresh home is not
attributed to the duties; what is counted is calls to ``_load_aggregate``.
"""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import follower_registration, runs, station_subscriber
from tests.helpers.git import git as _git

ledger_module = importlib.import_module("reckon.ledger")
obligations_module = importlib.import_module("reckon.crew.obligations")
recovery_classification = importlib.import_module("reckon.crew.recovery_classification")
recovery_review_dispatch = importlib.import_module(
    "reckon.crew.recovery_review_dispatch"
)
review_module = importlib.import_module("reckon.crew.review")

PROJECT = "review-duty-pass"
SESSION = "coordinator-review-duty-pass"
DELIVERED_RUNS = 3
DELIVERED_AGE = timedelta(days=3)
# One clock for the whole case, so the duty's own aged text is the same in both
# arms and only the read path can differ between them.
OBSERVED_AT = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def _observed_seconds() -> float:
    return OBSERVED_AT.timestamp()


def _commit(repository: Path, name: str, body: str) -> str:
    (repository / name).parent.mkdir(parents=True, exist_ok=True)
    (repository / name).write_text(body, encoding="utf-8")
    _git(repository, "add", name)
    _git(repository, "commit", "-q", "-m", f"test: {body.strip()}")
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Delivered runs that each owe a review of a head the store does not cover."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the review-duty fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = ledger_module.ledger_path(PROJECT).parent
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    ledger_module.ledger_path(PROJECT).write_text(
        json.dumps({"data": {"_version": 1, "members": [], "runs": [], "holds": []}}),
        encoding="utf-8",
    )

    pointers: list[dict[str, Any]] = []
    for index in range(DELIVERED_RUNS):
        run_id = f"run-{index:02d}"
        reviewed_head = _git(root, "rev-parse", "HEAD")
        worktree = tmp_path / f"worktree-{index:02d}"
        _git(root, "worktree", "add", "-q", "--detach", str(worktree), reviewed_head)
        head = _commit(worktree, f"reckon/mod_{index:02d}.py", f"VALUE = {index}\n")
        manifest = config_home / "manifests" / f"{run_id}.md"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            f"node: {run_id}\nstatus: complete\ncommits: [{head}]\n"
            f"changed_paths: [reckon/mod_{index:02d}.py]\n",
            encoding="utf-8",
        )
        # The classifier ages a terminal run from its manifest's mtime, and the
        # reflex only asks the ledger once the duty is past its review window.
        aged = (OBSERVED_AT - DELIVERED_AGE).timestamp()
        os.utime(manifest, (aged, aged))
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(root),
            "worktree": str(worktree),
            "base_sha": reviewed_head,
            "process_alive": False,
            "phase": "complete",
            "launch": "in-harness",
            "role": "implement",
            "manifest_path": str(manifest),
            "terminal_at": OBSERVED_AT.isoformat(),
            "node": {
                "id": run_id,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "write_paths": [f"reckon/mod_{index:02d}.py"],
            },
        }
        runs._write_json(runs.pointer_path(run_id), pointer)
        # The ledger holds each promoted run as its own record, which is the
        # membership the duty's closure answer reads.
        (ledger_dir / "runs" / f"{run_id}.json").write_text(
            json.dumps({"run_id": run_id, "project": PROJECT, "status": "complete"}),
            encoding="utf-8",
        )
        pointers.append(pointer)
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
        # A review of the head the run started from, not the head it delivered,
        # so the duty reads review-missing while the record stays readable.
        review_module.store_review(record)
    monkeypatch.setattr(obligations_module, "_utc_now", lambda: OBSERVED_AT)
    monkeypatch.setattr(recovery_classification, "_utc_seconds", _observed_seconds)
    return {"home": config_home, "repo": root, "pointers": pointers}


@pytest.fixture()
def live_follower(fleet: dict[str, Any]) -> Any:
    """Hold the session's delivery registration, so the reflex owns its reviews."""
    with follower_registration.follower_registration(
        PROJECT, SESSION, delivery="stream"
    ) as registration:
        assert registration.held
        yield registration


def _count_aggregate_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    """Record every call the pass makes to the aggregate reader."""
    calls: list[Any] = []
    real_load = ledger_module._load_aggregate

    def load(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(ledger_module, "_load_aggregate", load)
    return calls


def test_a_station_pass_reads_the_ledger_once(
    fleet: dict[str, Any],
    live_follower: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pass over every delivered run pays one aggregate read at most."""
    # Warm the ledger index so its build is not attributed to the duties.
    ledger_module.read_records(PROJECT)

    opened: list[dict[str, Any]] = []
    real_open = recovery_review_dispatch._open_review_refusal

    def open_refusal(record: dict[str, Any], **kwargs: Any) -> Any:
        opened.append(record)
        return real_open(record, **kwargs)

    monkeypatch.setattr(recovery_review_dispatch, "_open_review_refusal", open_refusal)
    duties: list[dict[str, Any]] = []
    real_duty = obligations_module._review_duty_item

    def duty(*args: Any, **kwargs: Any) -> Any:
        item = real_duty(*args, **kwargs)
        if item is not None:
            duties.append(item)
        return item

    monkeypatch.setattr(obligations_module, "_review_duty_item", duty)

    reads = _count_aggregate_reads(monkeypatch)
    station_subscriber.live_work_populations(fleet["pointers"])

    entered = {str(record.get("run_id")) for record in opened}
    assert entered == {pointer["run_id"] for pointer in fleet["pointers"]}, entered
    assert len(reads) <= 1, "a pass pays at most one aggregate read, not one per run"

    texts = [
        (
            str(item.get("kind") or ""),
            str(item.get("reason") or ""),
            str(item.get("next_command") or ""),
        )
        for item in duties
    ]
    assert len(texts) == DELIVERED_RUNS
    assert all(kind == "review-missing" for kind, _reason, _command in texts), texts
    assert all(command for _kind, _reason, command in texts), texts

    # The cache outlives the pass: a direct caller after it opens no read.
    before = len(reads)
    assert ledger_module.run_ids(PROJECT) == {
        pointer["run_id"] for pointer in fleet["pointers"]
    }
    assert len(reads) == before, "a later caller is served from the cached index"

    # The duties' text is what the ledger path produced before the change.
    def base_run_ids(project: str, root: Any = None) -> set[str]:
        data, _version = ledger_module._load_aggregate(project, root)
        return {
            str(record["run_id"])
            for record in data["runs"]
            if isinstance(record, dict) and record.get("run_id")
        } | {path.stem for path in ledger_module._run_files(project, root)}

    monkeypatch.setattr(ledger_module, "run_ids", base_run_ids)
    opened.clear()
    duties.clear()
    station_subscriber.live_work_populations(fleet["pointers"])
    base_texts = [
        (
            str(item.get("kind") or ""),
            str(item.get("reason") or ""),
            str(item.get("next_command") or ""),
        )
        for item in duties
    ]
    assert base_texts == texts, "the duty text does not move with the read path"
    assert {str(record.get("run_id")) for record in opened} == entered
    assert len(reads) - before, "the control path does read the aggregate per run"


def _runs_directory() -> Path:
    return ledger_module.ledger_path(PROJECT).parent / "runs"


def _write_run(run_id: str, status: str = "complete") -> Path:
    path = _runs_directory() / f"{run_id}.json"
    path.write_text(
        json.dumps({"run_id": run_id, "project": PROJECT, "status": status}),
        encoding="utf-8",
    )
    return path


def _indexed_status(run_id: str) -> Any:
    """The status the published index currently holds for one run."""
    path = ledger_module._run_index_path(PROJECT, None)
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM records WHERE name=?", (run_id,)
        ).fetchone()
    return None if row is None or row[0] is None else json.loads(row[0])["status"]


def test_a_run_file_added_after_the_read_is_membership(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file written after the warm read is counted, with no aggregate parse.

    The index is current only while it describes the files the directory holds;
    the added file breaks that, so the read refreshes. A read served stale would
    drop the new id, and a read that rebuilt from the aggregate would pay a
    parse the station tick cannot afford.
    """
    ledger_module.read_records(PROJECT)
    reads = _count_aggregate_reads(monkeypatch)
    _write_run("run-added")
    assert ledger_module.run_ids(PROJECT) == {
        pointer["run_id"] for pointer in fleet["pointers"]
    } | {"run-added"}
    assert not reads, "a stale index is refreshed without parsing the aggregate"


def test_a_run_file_rewritten_in_place_is_not_served_stale(
    fleet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-place edit is noticed and refreshed, not answered from the old row.

    Directory metadata cannot see this: the name is unchanged, so only the
    file's own stat identity shows the edit. Membership is the same either way,
    so the evidence is the index's stored row, which must carry the rewritten
    status rather than the one it held before.
    """
    ledger_module.read_records(PROJECT)
    assert _indexed_status("run-00") == "complete"
    reads = _count_aggregate_reads(monkeypatch)
    _write_run("run-00", status="blocked")
    assert "run-00" in ledger_module.run_ids(PROJECT)
    assert _indexed_status("run-00") == "blocked", "the index was served stale"
    assert not reads, "an in-place edit is refreshed without an aggregate parse"
