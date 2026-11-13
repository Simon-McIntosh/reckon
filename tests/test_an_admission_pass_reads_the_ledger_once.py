"""Section waits share one indexed history snapshot for each release stage."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import queue_admission, runs
from reckon.crew.admission import release
from tests import test_a_wait_releases_when_its_upstream_lands as fixtures

PROJECT = fixtures.PROJECT
PLAN = fixtures.PLAN
SECTIONS = [f"s{index + 1}" for index in range(5)]


@pytest.fixture()
def history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repository = fixtures.queue_repo.__wrapped__(tmp_path, monkeypatch)
    landed = fixtures._git(repository, "rev-parse", "HEAD")
    tree = fixtures._git(repository, "rev-parse", "HEAD^{tree}")
    pending = fixtures._git(
        repository, "commit-tree", tree, "-p", landed, "-m", "pending delivery"
    )
    declarations = dict.fromkeys(SECTIONS, "done")
    requests = []
    waits = {}
    for index in range(10):
        own = f"waiter{index}"
        declarations[own] = "implementable"
        waits[own] = [f"{PLAN}#{SECTIONS[index % len(SECTIONS)]}"]
        requests.append(
            {
                "run_id": f"r-waiter-{index}",
                "project": PROJECT,
                "repo": str(repository),
                "phase": "awaiting-upstream",
                "node": {"id": own, "plan": PLAN, "section": own},
                "waits": waits[own],
            }
        )
    path = repository / "docs" / "plans" / f"{PLAN}.html"
    path.write_text(fixtures._plan_html(declarations, waits), encoding="utf-8")
    rows = [
        {"run_id": f"r-history-{index}", "plan": "elsewhere", "section": "s1"}
        for index in range(1995)
    ]
    for name, section, revision in (
        ("landed", "s1", landed),
        ("pending", "s2", pending),
        ("discarded", "s4", pending),
        ("reachable", "s5", landed),
        ("unreachable", "s5", pending),
    ):
        rows.append(
            {
                "run_id": f"r-{name}",
                "plan": PLAN,
                "section": section,
                "promoted_revision": revision,
            }
        )
    directory = ledger.run_path(PROJECT, "seed").parent
    directory.mkdir(parents=True, exist_ok=True)
    for row in rows:
        (directory / f"{row['run_id']}.json").write_text(ledger.serialize_run(row))
    observed, _version = ledger.load(PROJECT)
    assert len(observed["runs"]) == 2000
    assert {row["run_id"] for row in observed["runs"]} == {
        row["run_id"] for row in rows
    }
    monkeypatch.setattr(release, "_discarded", lambda run_id: run_id == "r-discarded")
    return repository, requests, pending


def _store(requests):
    pointers = copy.deepcopy(requests)
    for pointer in pointers:
        queue_admission._write_pointer(pointer["run_id"], pointer)
    return pointers


def _phases(pointers):
    return [runs.read_pointer(pointer["run_id"])["phase"] for pointer in pointers]


def _legacy_verdicts(monkeypatch, requests):
    """Resolve through the helper's original whole-ledger read for each section."""
    original = runs._unloaded_sections

    def unindexed(project, pointers, plans, **_options):
        return original(project, pointers, plans)

    with monkeypatch.context() as patch:
        patch.setattr(runs, "_unloaded_sections", unindexed)
        return [
            release.resolve_waits(
                pointer,
                live=requests,
                docs_dir=Path(pointer["repo"]) / "docs",
                cache={},
            )
            for pointer in copy.deepcopy(requests)
        ]


def _count_reads(monkeypatch, directory):
    counts = {"loads": 0, "unindexed": 0, "run_documents": 0, "run_document_opens": 0}
    original_load = ledger.load
    original_open = Path.open
    original_read_run = ledger._read_run

    def read_run(path):
        counts["run_documents"] += 1
        return original_read_run(path)

    def load(*args, **kwargs):
        counts["loads"] += 1
        counts["unindexed"] += not kwargs.get("use_index", True)
        return original_load(*args, **kwargs)

    def opened(path, *args, **kwargs):
        if path.parent == directory and path.suffix == ".json":
            counts["run_document_opens"] += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(ledger, "load", load)
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(ledger, "_read_run", read_run)
    return counts


def test_one_release_stage_reads_history_once(history, monkeypatch):
    _repository, requests, _pending = history
    expected = _legacy_verdicts(monkeypatch, requests)
    assert set(expected) == {"queued", "awaiting-upstream"}
    assert expected == ["queued", "awaiting-upstream", "queued", "queued", "queued"] * 2
    pointers = _store(requests)
    counts = _count_reads(monkeypatch, ledger.run_path(PROJECT, "seed").parent)
    result = release.advance_waiting_requests(pointers)
    assert _phases(pointers) == expected
    assert len(result["promoted"]) == 8
    print("release read counts:", json.dumps(counts, sort_keys=True))
    assert counts["loads"] == 1, counts
    assert counts["unindexed"] == 0, counts
    assert counts["run_documents"] == 0, counts
    assert counts["run_document_opens"] == 0, counts


def test_a_promotion_during_the_pass_is_seen_next_pass(history, monkeypatch):
    _repository, requests, _pending = history
    pointers = _store(requests)
    counts = _count_reads(monkeypatch, ledger.run_path(PROJECT, "seed").parent)
    original_load = ledger.load
    changed = False

    def land_after_snapshot(*args, **kwargs):
        nonlocal changed
        snapshot = original_load(*args, **kwargs)
        if not changed:
            changed = True
            # Publish the landing after the snapshot is read, while HEAD stays
            # fixed, so only a new ledger reading can observe this promotion.
            ledger.write_run_file(
                PROJECT,
                {
                    "run_id": "r-pending",
                    "plan": PLAN,
                    "section": "s2",
                    "promoted_revision": fixtures._git(history[0], "rev-parse", "HEAD"),
                },
            )
        return snapshot

    monkeypatch.setattr(ledger, "load", land_after_snapshot)
    release.advance_waiting_requests(pointers)
    assert _phases(pointers)[1::5] == ["awaiting-upstream"] * 2
    assert counts["loads"] == 1, counts
    release.advance_waiting_requests(pointers)
    assert _phases(pointers) == ["queued"] * 10
    assert counts["loads"] == 2, counts
    assert counts["unindexed"] == 0, counts
    print("two-pass read counts:", json.dumps(counts, sort_keys=True))


def test_a_landing_during_the_pass_is_seen_next_pass(history, monkeypatch):
    repository, requests, pending = history
    pointers = _store(requests)
    counts = _count_reads(monkeypatch, ledger.run_path(PROJECT, "seed").parent)
    original = release._resolve_and_move
    merged = False

    def land_after_wait(pointer, **options):
        nonlocal merged
        phase = original(pointer, **options)
        if not merged and pointer["run_id"] == "r-waiter-1":
            assert phase == "awaiting-upstream"
            fixtures._git(repository, "merge", "--ff-only", pending)
            assert fixtures._git(repository, "rev-parse", "HEAD") == pending
            merged = True
        return phase

    monkeypatch.setattr(release, "_resolve_and_move", land_after_wait)
    release.advance_waiting_requests(pointers)
    assert merged
    assert _phases(pointers)[1::5] == ["awaiting-upstream"] * 2
    assert counts["loads"] == 1, counts
    release.advance_waiting_requests(pointers)
    assert _phases(pointers) == ["queued"] * 10
    assert counts["loads"] == 2, counts
    assert counts["unindexed"] == 0, counts
    assert counts["run_documents"] == 0, counts
    assert counts["run_document_opens"] == 0, counts
