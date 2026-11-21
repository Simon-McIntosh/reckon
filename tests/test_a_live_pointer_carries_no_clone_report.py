"""A live pointer carries no clone report, and a listing parses none.

The promotion's clone report is evidence of a run: every function the run added
or modified whose normalised window duplicates one already in the tree. It is
committed on the run's ledger row, which is where ``velocity`` and every other
history reader take it. A live pointer is a different thing — every fleet-state
derivation on the node lists the live directory on every pass, and none of them
asks for the report. Promotion retires the pointer, and a landing cannot
recreate it. Permitted pointer writes and listings omit the report.

All checks use a temporary crew home. A promoted row keeps its report without
recreating its pointer, and an unpromoted pointer written from report-bearing
data carries no report key. A pointer file that already holds a 5 MB report is
listed without it and parsed for under 1 MB. ``velocity``'s clone figures for
the run are the ones its report states.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon import clones, crew, ledger, velocity
from reckon.crew import pending_landing, runs
from reckon.crew.runs import pointer_path
from tests.helpers.git import git as _git

PROJECT = "sample"

#: The report every test here hands the pointer and the row. It is built to be
#: the size the profile measured rather than a token value, because the defect
#: was the cost of decoding it and a small list would not show that cost.
REPORT_BYTES = 5_000_000
REPORT_ENTRY_BYTES = 250


def _clone_report() -> list[dict]:
    """A clone report of at least ``REPORT_BYTES`` once written as JSON."""
    padding = "x" * (REPORT_ENTRY_BYTES - 120)
    entry = {
        "existing_function": {
            "path": "reckon/_timestamps.py",
            "name": "parse_utc",
            "line": 12,
        },
        "run_function": {
            "path": "reckon/private_copy.py",
            "name": "copy_parse_utc",
            "line": 3,
        },
        "window": padding,
    }
    return [dict(entry) for _ in range(REPORT_BYTES // REPORT_ENTRY_BYTES)]


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway project checkout with its own crew home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "reckon").mkdir()
    (root / "reckon" / "_timestamps.py").write_text(
        '"""A module the base carries."""\n\n\n'
        "def parse_utc(value):\n"
        "    return value\n",
        encoding="utf-8",
    )
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "reckon"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _run_pointer(repository: Path, run_tree: Path, run_id: str, base: str) -> None:
    """Write the pointer a dispatch leaves for a run that is about to deliver."""
    pointer_path(run_id).parent.mkdir(parents=True, exist_ok=True)
    pointer_path(run_id).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "repo": str(repository),
                "worktree": str(run_tree),
                "base_sha": base,
                "launch": "in-harness",
                "role": "implement",
                "backend": "native",
                "created_at": "2026-11-14T10:00:00Z",
                "node": {
                    "id": "clone-report",
                    "plan": "fixture",
                    "section": "guard",
                    "time_budget": "25m",
                    "write_paths": ["reckon/private_copy.py"],
                },
            }
        ),
        encoding="utf-8",
    )


def _promote_with_report(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, dict]:
    """Promote one run whose detector measured the oversized report."""
    report = _clone_report()
    monkeypatch.setattr(
        clones, "promotion_clone_matches", lambda *args, **kwargs: report
    )
    base = _git(repository, "rev-parse", "HEAD")
    run_tree = tmp_path / "run-tree"
    _git(repository, "worktree", "add", "-q", "--detach", str(run_tree), "HEAD")
    run_id = "r-live-pointer-report"
    (run_tree / "reckon" / "private_copy.py").write_text(
        "def copy_parse_utc(value):\n    return value\n", encoding="utf-8"
    )
    _git(run_tree, "add", "reckon/private_copy.py")
    _git(run_tree, "commit", "-q", "-m", "test: add a private copy")
    commit = _git(run_tree, "rev-parse", "HEAD")
    _run_pointer(repository, run_tree, run_id, base)
    receipt = crew.complete(run_id, gate="passed", commits=[commit], root=repository)
    return run_id, receipt


def _ledger_row(repository: Path, run_id: str) -> dict:
    data, _version = ledger.load(PROJECT, root=repository)
    row = next(
        (item for item in data["runs"] if str(item.get("run_id") or "") == run_id),
        None,
    )
    assert row is not None, "the promotion did not commit a ledger row"
    return dict(row)


class _RecordingJSON:
    """A stand-in for the module ``runs`` decodes pointer text with."""

    def __init__(self, module) -> None:
        self._module = module
        self.decoded: list[str] = []

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    def loads(self, value, *args, **kwargs):
        self.decoded.append(value)
        return self._module.loads(value, *args, **kwargs)


def test_a_promoted_run_keeps_its_report_on_the_row_and_not_on_its_pointer(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row retains its report while permitted live writes omit it."""
    run_id, receipt = _promote_with_report(repository, tmp_path, monkeypatch)
    row = receipt["record"]
    assert row["clone_matches"], "the promotion recorded no clone report at all"
    assert len(json.dumps(row["clone_matches"])) >= REPORT_BYTES

    assert not pointer_path(run_id).exists()
    with pytest.raises(runs.CrewError, match="cannot write a live pointer"):
        pending_landing.mark_pending_landing_repair(run_id, dict(row))
    assert not pointer_path(run_id).exists()

    unpromoted_id = "r-unpromoted-pointer-report"
    unpromoted = {**dict(row), "run_id": unpromoted_id, "project": PROJECT}
    assert pending_landing._promoted_row_absent(PROJECT, unpromoted_id)
    pending_landing.mark_pending_landing_repair(unpromoted_id, unpromoted)
    written = json.loads(pointer_path(unpromoted_id).read_text())
    assert written.get("pending_landing_repair") is True
    assert "clone_matches" not in written, (
        "the live pointer carries the promotion's clone report"
    )
    stored = _ledger_row(repository, run_id)
    assert len(json.dumps(stored["clone_matches"])) >= REPORT_BYTES


def test_a_pointer_that_already_carries_the_report_is_listed_without_parsing_it(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A pre-existing pointer is listed without the key, parsed for < 1 MB."""
    del repository
    report = _clone_report()
    run_id = "r-pointer-already-carries-it"
    path = pointer_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "phase": "complete",
                "created_at": "2026-11-14T10:00:00Z",
                "clone_matches": report,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    assert path.stat().st_size >= REPORT_BYTES, (
        "the pointer the listing reads is smaller than the report it is meant to carry"
    )

    recorder = _RecordingJSON(json)
    monkeypatch.setattr(runs, "json", recorder)
    # Positive control: the instrument sees a decode of this pointer at the size
    # the file is, so a small figure below is a reading and not a blind spot.
    runs.json.loads(path.read_text())
    assert max(len(text) for text in recorder.decoded) >= REPORT_BYTES
    recorder.decoded.clear()

    records = runs.list_live()
    listed = next((item for item in records if item["run_id"] == run_id), None)
    assert listed is not None, "the listing dropped the pointer it was reading"
    parsed = sum(len(text) for text in recorder.decoded if run_id in text)
    assert parsed > 0, "the listing parsed nothing for this pointer at all"
    assert parsed < 1_000_000, f"the listing parsed {parsed} bytes for this pointer"
    assert "clone_matches" not in listed, "the listing returned the clone report"


def test_velocitys_clone_figures_for_the_run_read_the_row(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) ``velocity``'s clone figures state the run's own report, unchanged."""
    run_id, receipt = _promote_with_report(repository, tmp_path, monkeypatch)
    row = _ledger_row(repository, run_id)
    report = row["clone_matches"]
    assert len(json.dumps(report)) >= REPORT_BYTES
    day = datetime.fromisoformat(str(row["dispatched_at"]))
    assert day.tzinfo is not None
    window_start = (day - timedelta(days=1)).astimezone(UTC).isoformat()
    window_end = (day + timedelta(days=1)).astimezone(UTC).isoformat()

    cells = velocity._clone_week_cells(
        [{**row, "day": day.date().isoformat()}], window_start, window_end
    )

    assert len(cells) == 1, cells
    cell = cells[0]
    assert cell["runs_with_match"] == 1, cell
    assert cell["matches"] == len(report), cell
    assert cell["runs_no_match"] == 0 and cell["runs_unmeasured"] == 0, cell
    assert receipt["record"]["clone_matches"] == report
