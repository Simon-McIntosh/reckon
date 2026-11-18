"""A resumption pass shares one live listing and refreshes it on the next pass."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew import open_run_index, recovery, resumption, runs
from reckon.crew import recovery_classification as classification
from reckon.crew import recovery_review_acceptance as acceptance

PROJECT = "sweep-fixture"
SESSION = "sweep-owner"
POPULATION = 200
NOW = datetime(2030, 1, 1, tzinfo=UTC)
HEAD = "a" * 40


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    # Publishing a watch stream is a separate consumer of this listing. This
    # measure drives the resumption pass itself without arming another pass.
    monkeypatch.setattr(runs, "_publish_watch_stream", lambda *a, **k: None)
    monkeypatch.setattr(classification, "_utc_seconds", NOW.timestamp)
    monkeypatch.setattr(resumption, "_utc_now", NOW.isoformat)
    monkeypatch.setattr(
        resumption.plan_review, "compose_owed_plan_reviews", lambda *a, **k: {}
    )
    # There is no repository diff in this fixture: all completed runs are
    # source deliveries that owe a review, so the real deferral guard is reached.
    monkeypatch.setattr(acceptance, "_sweep_review_tier", lambda *a, **k: "full")
    kinds = (
        ("queued", ""),
        ("held", ""),
        ("awaiting-upstream", ""),
        ("blocked-upstream", ""),
        ("working", "in-progress"),
        ("stopped", ""),
        ("complete", "blocked"),
        ("complete", "failed"),
        ("working", "waiting"),
        ("starting", ""),
    )
    originals = []
    for index in range(POPULATION):
        phase, status = (
            kinds[index - 160]
            if index >= 160 and index < 170
            else ("complete", "complete")
        )
        if index >= 170:
            phase, status = kinds[(index - 170) % len(kinds)]
        pointer = _pointer(home, f"r-{index:03d}", phase=phase, status=status)
        originals.append(pointer)
    return home, originals


def _pointer(home: Path, run_id: str, *, phase="complete", status="complete"):
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(exist_ok=True)
    if status:
        body = f"node: {run_id}\nstatus: {status}\n"
        if status == "complete":
            body += f"commits: [{HEAD}]\n"
        if status == "waiting":
            body += (
                "wait_condition: the fixture condition ends\n"
                'wait_probe: ["true"]\nwait_terminal: exit:0\n'
                "resume_brief: continue the fixture\n"
            )
        manifest.write_text(body)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "session": SESSION,
        "role": "implement",
        "phase": phase,
        "launch": "in-harness",
        "launcher_host": "a-foreign-fixture-host",
        "process_alive": False,
        "base_sha": HEAD,
        "manifest_path": str(manifest),
        "created_at": "2029-12-31T23:59:00Z",
        "acknowledgement": {"until": "2099-01-01T00:00:00Z", "reason": "fixture"},
        "node": {"id": f"node-{run_id}", "write_paths": []},
    }
    runs._write_json(runs.pointer_path(run_id), record)
    return record


@contextmanager
def _counter(monkeypatch):
    reads = Counter()
    read = runs._pointer_text

    def counted(path):
        reads[path.name] += 1
        return read(path)

    with monkeypatch.context() as patch:
        patch.setattr(runs, "_pointer_text", counted)
        yield reads


def _sweep():
    return resumption.sweep(
        PROJECT,
        session=SESSION,
        config={},
        now=NOW,
        condition_test=lambda *a: {"terminal": False, "observed": "pending"},
    )


def _observations(report, originals):
    actions = {key: report[key] for key in ("checked", "resumed", "skipped", "reviews")}
    assert "error" not in actions["reviews"], actions
    assert not actions["reviews"].get("failed"), actions
    rows = {}
    for pointer in originals:
        row = recovery.classify_pointer(pointer, now_seconds=NOW.timestamp())
        rows[pointer["run_id"]] = {
            key: row.get(key)
            for key in ("classification", "recovery_classification", "action")
        }
    return {"actions": actions, "classifications": rows}


def _reset(originals):
    for pointer in originals:
        runs._write_json(runs.pointer_path(pointer["run_id"]), pointer)


def _reference(monkeypatch, originals):
    """Force independent reads at each query, the unshared listing semantics."""
    _reset(originals)
    with monkeypatch.context() as patch:
        patch.setattr(open_run_index, "pointers", lambda **kwargs: None)
        report = _sweep()
        return _observations(report, originals)


def test_a_sweep_bounds_pointer_decodes_and_preserves_its_answers(fleet, monkeypatch):
    _home, originals = fleet
    with _counter(monkeypatch) as reads:
        report = _sweep()
    observed = _observations(report, originals)
    reference = _reference(monkeypatch, originals)
    assert observed == reference
    # A positive control: the sweep really reached the deferral guard and the
    # fixture contains all its parked, active and delivered phase spellings.
    assert len(observed["classifications"]) == POPULATION
    assert len(report["reviews"]["refused"]) == 160
    assert {pointer["phase"] for pointer in originals} >= {
        "queued",
        "held",
        "awaiting-upstream",
        "blocked-upstream",
        "working",
        "stopped",
        "complete",
        "starting",
    }
    digest = hashlib.sha256(json.dumps(observed, sort_keys=True).encode()).hexdigest()
    print(
        f"SWEEP pointer_decodes={reads.total()} population={POPULATION} answers={digest}"
    )
    assert reads.total() >= POPULATION, "the instrument missed the initial listing"
    assert reads.total() <= 3 * POPULATION, (
        f"one sweep decoded {reads.total()} pointers for {POPULATION} live runs"
    )


def test_a_pointer_written_during_a_sweep_is_seen_by_the_next(fleet, monkeypatch):
    home, originals = fleet
    classify = resumption.classify_pointer
    wrote = []

    def classify_and_write(record, **kwargs):
        if not wrote:
            wrote.append(_pointer(home, "r-added-during-pass"))
        return classify(record, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(resumption, "classify_pointer", classify_and_write)
        first = _sweep()
    assert wrote, "the sweep never reached classification"
    first_ids = {row["run_id"] for row in first["reviews"]["reports"]}
    assert wrote[0]["run_id"] not in first_ids
    with _counter(monkeypatch) as reads:
        second = _sweep()
    second_observations = _observations(second, [*originals, *wrote])
    assert wrote[0]["run_id"] in {row["run_id"] for row in second["reviews"]["refused"]}
    assert second_observations == _reference(monkeypatch, [*originals, *wrote])
    assert reads["r-added-during-pass.json"] >= 1
    print(f"NEXT_SWEEP pointer_decodes={reads.total()} population={POPULATION + 1}")
    assert reads.total() <= 3 * (POPULATION + 1)
