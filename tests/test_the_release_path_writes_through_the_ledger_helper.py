"""A release rewrites its run file through the ledger's one run-file writer.

``ledger.write_run_file`` is the one owner of rewriting a run's own file. The
release path must reach it rather than encoding the row itself, so the test
patches the helper and requires it to be called once with the run's row, and
checks the file's bytes are the ones the ledger's serialiser produced.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

from reckon import ledger
from reckon.crew.promotion_release import (
    _record_release_on_ledger,
    _update_run_record,
)


def _project_with_split_run(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    repo = tmp_path / "repository"
    aggregate = ledger.ledger_path("sample", repo)
    aggregate.parent.mkdir(parents=True)
    ledger.write("sample", {"members": [], "runs": [], "holds": []}, 0, root=repo)
    target = ledger.run_path("sample", "split-0", repo)
    target.parent.mkdir(exist_ok=True)
    target.write_text(ledger.serialize_run({"run_id": "split-0", "worktree": "/w"}))
    return repo, target


def _spy_write_run_file(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = ledger.write_run_file

    def spy(project, row, root=None, **kwargs):
        calls.append({"project": project, "row": dict(row), "root": root})
        return real(project, row, root, **kwargs)

    monkeypatch.setattr(ledger, "write_run_file", spy)
    return calls


def test_release_writes_the_run_file_through_the_ledger_helper(tmp_path, monkeypatch):
    repo, target = _project_with_split_run(tmp_path, monkeypatch)
    calls = _spy_write_run_file(monkeypatch)
    receipt = {"worktree_released": True}

    recorded, row = _record_release_on_ledger(
        project="sample",
        root=repo,
        run_id="split-0",
        release=receipt,
        checkout=None,
    )

    assert recorded == receipt
    assert len(calls) == 1, f"helper called {len(calls)} times, expected once"
    assert calls[0]["project"] == "sample"
    assert ledger.run_path("sample", "split-0", calls[0]["root"]) == target
    expected = {"run_id": "split-0", "worktree": "/w", "release": receipt}
    assert calls[0]["row"] == expected
    assert row == expected
    # The base tree produced the same bytes: write_run_file renders the row
    # through the same serialiser the release path used before the change.
    assert target.read_bytes() == ledger.serialize_run(expected).encode("utf-8")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_run_record_update_resolves_a_config_home_run_file(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    target = ledger.run_path("sample", "home-0", None)
    target.parent.mkdir(parents=True)
    target.write_text(ledger.serialize_run({"run_id": "home-0"}))
    calls = _spy_write_run_file(monkeypatch)

    updated = _update_run_record(target, "home-0", {"release": {"x": 1}})
    assert updated == {"run_id": "home-0", "release": {"x": 1}}
    assert len(calls) == 1
    assert calls[0]["root"] is None
    assert json.loads(target.read_text(encoding="utf-8"))["release"] == {"x": 1}
