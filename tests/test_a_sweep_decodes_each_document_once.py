"""One sweep decodes each crew document at most once, and reuses it afterwards.

The producer derives the fleet about once a minute in one long-lived process,
and every reader in that derivation reached the same per-run documents through
its own reader — the held-worktree scan and the ledger's own index build each
decoded every run file again. A document is now held against the stat identity
of the file it was read from, so one sweep decodes it once and a later sweep
skips it while the file has not moved. This pins the three facts that makes
true: the first sweep decodes each document at most once, a second sweep in the
same process decodes only the one file that changed, and the memo changes
nothing about the snapshots either sweep writes.

The counts are taken over the documents the fixture itself wrote — the per-run
ledger files and the live pointers — by matching each ``json.loads`` argument
against the text of those files. The crew home's configuration and its ledger
index are read by other modules outside this node's writer and are not counted
here.
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

PROJECT = "decode-fixture"
SESSION = "coordinator-fixture"
RUN_FILES = 200
LIVE_POINTERS = 20


def _now() -> datetime:
    return datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


def _clear_memo() -> None:
    """Start a test from no held document.

    The memo is process-wide, so a test that leaves entries behind — the bound
    test changes the limit — would otherwise be read against them. Missing
    attributes are tolerated: the negative control runs this file against the
    base revision, which has no memo at all.
    """
    ledger_module.__dict__.get("_DOCUMENT_CACHE", {}).clear()
    ledger_module.__dict__.get("_OPENED_SINCE_RELEASE", set()).clear()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A synthesised project of 200 per-run files and 20 live pointers."""
    _clear_memo()
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
        ("commit", "-q", "-m", "test: seed the decode fixture"),
    ):
        _git(root, *arguments)
    head = _git(root, "rev-parse", "HEAD")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    ledger_dir = root / "docs" / "state" / PROJECT
    (ledger_dir / "runs").mkdir(parents=True, exist_ok=True)
    for index in range(RUN_FILES):
        run_id = f"r-{index:04d}"
        (ledger_dir / "runs" / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "node": run_id,
                    "plan": "fixture-plan",
                    "project": PROJECT,
                    "completed_at": f"2026-10-01T{index % 24:02d}:00:00Z",
                }
            ),
            encoding="utf-8",
        )
    for index in range(LIVE_POINTERS):
        run_id = f"r-live-{index:03d}"
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
                "phase": "working",
                "launch": "in-harness",
                "role": "implement",
                "manifest_path": str(config_home / "manifests" / f"{run_id}.md"),
                "node": {"id": run_id, "plan": "fixture-plan", "section": "s"},
            },
        )
    return {"home": config_home, "repo": root, "ledger": ledger_dir}


def _run_sweep(fleet: dict[str, Path]) -> None:
    """Drive exactly one forced sweep and let it publish."""
    obligation_snapshot.sweep(
        PROJECT,
        sessions=[SESSION],
        producer={
            "pid": 1,
            "pid_start_time": "1",
            "started_at": None,
            "code_stamp": "decode-fixture",
        },
        stream_offset=0,
        transition_fired=True,
        state_dirs=[],
        now=_now(),
    )


class _DecodeCounter:
    """Count ``json.loads`` decodes of the documents a fixture wrote."""

    def __init__(self, documents: dict[str, str]) -> None:
        # keyed by the file's exact text -> its path, so a decode of one
        # document is attributed however many readers asked for it.
        self._text_to_path = documents
        self.per_document: dict[str, int] = {}
        self.total = 0

    def __enter__(self) -> Self:
        self._real = json.loads
        counter = self

        def counting(text: Any, *args: Any, **kwargs: Any) -> Any:
            path = counter._text_to_path.get(text) if isinstance(text, str) else None
            if path is not None:
                counter.total += 1
                counter.per_document[path] = counter.per_document.get(path, 0) + 1
            return counter._real(text, *args, **kwargs)

        json.loads = counting
        return self

    def __exit__(self, *exc: object) -> None:
        json.loads = self._real


def _document_texts(paths: list[Path]) -> dict[str, str]:
    return {path.read_text(encoding="utf-8"): str(path) for path in paths}


def _per_run_paths(fleet: dict[str, Path]) -> list[Path]:
    return sorted((fleet["ledger"] / "runs").glob("*.json"))


def test_one_sweep_decodes_each_document_once(fleet: dict[str, Path]) -> None:
    """A first sweep decodes no document more often than it opens it."""
    run_paths = _per_run_paths(fleet)
    assert len(run_paths) >= RUN_FILES
    documents = _document_texts(run_paths)

    with _DecodeCounter(documents) as counter:
        _run_sweep(fleet)

    assert counter.total <= len(counter.per_document), (
        f"a sweep decoded {counter.total} run documents from {len(counter.per_document)} files:"
        f" {sorted(counter.per_document.items(), key=lambda kv: -kv[1])[:5]}"
    )


def test_a_second_sweep_decodes_only_the_changed_file(fleet: dict[str, Path]) -> None:
    """A later sweep re-decodes one per-run file, not all of them."""
    _run_sweep(fleet)
    changed = _per_run_paths(fleet)[5]
    payload = json.loads(changed.read_text(encoding="utf-8"))
    payload["completed_at"] = "2026-10-02T00:00:00Z"
    changed.write_text(json.dumps(payload), encoding="utf-8")
    documents = _document_texts(_per_run_paths(fleet))

    with _DecodeCounter(documents) as counter:
        _run_sweep(fleet)

    assert counter.total == 1, (
        f"a second sweep decoded {counter.total} per-run documents,"
        f" expected exactly the one that changed: {counter.per_document}"
        f" (changed={changed.name})"
    )
    assert counter.per_document == {str(changed): 1}


def test_the_memoised_sweep_matches_an_unmemoised_one(fleet: dict[str, Path]) -> None:
    """The snapshot is unchanged whether the documents are held or re-read."""
    _run_sweep(fleet)
    held = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert held is not None
    # Drop every held document, so the next sweep decodes the whole state again
    # rather than reusing the first sweep's parse.
    _clear_memo()
    _run_sweep(fleet)
    reread = obligation_snapshot.read_snapshot(PROJECT, SESSION)
    assert reread == held


def test_a_sweep_drops_the_documents_it_did_not_open(fleet: dict[str, Path]) -> None:
    """A file removed between sweeps does not keep its parse in the memo."""
    _run_sweep(fleet)
    removed = _per_run_paths(fleet)[:40]
    keys = {str(path) for path in removed}
    assert keys <= set(ledger_module._DOCUMENT_CACHE)

    for path in removed:
        path.unlink()
    _run_sweep(fleet)

    assert not (keys & set(ledger_module._DOCUMENT_CACHE)), (
        "a sweep kept documents it did not open:"
        f" {sorted(keys & set(ledger_module._DOCUMENT_CACHE))[:5]}"
    )


def test_the_memo_stays_within_its_bound_across_changing_files(
    fleet: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held documents are bounded however many sweeps run over the state."""
    monkeypatch.setattr(ledger_module, "DOCUMENT_CACHE_LIMIT", 64)
    for round_number in range(6):
        paths = _per_run_paths(fleet)
        # Move a different slice each round, so the population keeps changing
        # and a memo that only ever grew would show it.
        for path in paths[round_number::17]:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["completed_at"] = f"2026-10-0{round_number + 1}T00:00:00Z"
            path.write_text(json.dumps(payload), encoding="utf-8")
        _run_sweep(fleet)
        assert (
            len(ledger_module._DOCUMENT_CACHE) <= ledger_module.DOCUMENT_CACHE_LIMIT
        ), f"round {round_number} held {len(ledger_module._DOCUMENT_CACHE)} documents"
