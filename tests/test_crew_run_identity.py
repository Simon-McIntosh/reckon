"""Run rows use the recovery resolver and explain recovery eligibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from reckon import crew, ledger
from reckon.crew.query import runs_view
from reckon.crew.resumption import resolve_session

PROJECT = "proj"
SESSION_STREAM = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "backends"
    / "codex-usage-limit.jsonl"
)


@pytest.fixture()
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _write_pointer(
    home: Path,
    repository: Path,
    run_id: str,
    *,
    session_id: str | None = None,
    stream_session: bool = False,
    worktree_exists: bool = True,
    process_alive: bool = False,
) -> dict:
    run_directory = home / "runs" / run_id
    run_directory.mkdir(parents=True)
    stream = run_directory / "stream.jsonl"
    if stream_session:
        stream.write_bytes(SESSION_STREAM.read_bytes())
    else:
        stream.write_text("", encoding="utf-8")
    worktree = home / "worktrees" / run_id
    if worktree_exists:
        worktree.mkdir(parents=True)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repository),
        "node": {"id": "node-a", "plan": "plan-a"},
        "phase": "working",
        "process_alive": process_alive,
        "worktree": str(worktree),
        "launch": "cli",
        "argv": ["codex", "exec"],
        "backend": "alpha",
        "log_path": str(stream),
        "manifest_path": str(run_directory / "manifest.md"),
    }
    if session_id is not None:
        record["session_id"] = session_id
    crew._write_json(crew.pointer_path(run_id), record)
    return record


def _only_row(repository: Path, *, source: str = "live") -> dict:
    result = runs_view(PROJECT, checkout_path=str(repository), source=source)
    assert result["count"] == 1
    return result["rows"][0]


def test_stream_session_matches_the_shared_resolver(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    run_id = "run-stream"
    pointer = _write_pointer(
        isolated_reckon_home,
        repository,
        run_id,
        stream_session=True,
    )

    expected = resolve_session(
        run_id,
        record=pointer,
        project=PROJECT,
        root=repository,
    )
    row = _only_row(repository)

    assert expected["resolved"] is True
    assert row["session_id"] == expected["session_id"]
    assert row["session_id_source"] == expected["source"] == "stream"


def test_promoted_session_resolves_from_the_ledger(
    repository: Path,
) -> None:
    ledger.append_run(
        PROJECT,
        ledger.build_record(
            run_id="run-promoted",
            plan="plan-a",
            node="node-a",
            gate="passed",
            session_id="session-ledger",
        ),
        root=repository,
    )

    row = _only_row(repository, source="ledger")

    assert row["session_id"] == "session-ledger"
    assert row["session_id_source"] == "ledger"
    assert row["resumable"] is False
    assert row["resumable_reason"] == "worktree released by promotion"


def test_pointer_session_short_circuits_stream_reading(
    isolated_reckon_home: Path,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_pointer(
        isolated_reckon_home,
        repository,
        "run-pointer",
        session_id="session-pointer",
    )

    def _unexpected_stream_read(_record: dict) -> str:
        raise AssertionError("the stream was read after the pointer answered")

    monkeypatch.setattr(
        "reckon.crew.resumption._stream_session",
        _unexpected_stream_read,
    )

    row = _only_row(repository)

    assert row["session_id"] == "session-pointer"
    assert row["session_id_source"] == "pointer"


def test_absent_session_names_every_consulted_source(
    isolated_reckon_home: Path,
    repository: Path,
) -> None:
    run_id = "run-without-session"
    pointer = _write_pointer(isolated_reckon_home, repository, run_id)

    expected = resolve_session(
        run_id,
        record=pointer,
        project=PROJECT,
        root=repository,
    )
    row = _only_row(repository)

    assert expected["consulted"] == ["pointer", "stream", "ledger"]
    assert row["session_id"] is None
    assert row["session_id_source"] is None
    assert row["resumable"] is False
    assert row["resumable_reason"] == expected["detail"]
    assert all(source in row["resumable_reason"] for source in expected["consulted"])


@pytest.mark.parametrize(
    ("worktree_exists", "process_alive", "resumable", "reason"),
    [
        (False, False, False, "worktree released by promotion"),
        (True, True, False, "the run's process is alive"),
        (
            True,
            False,
            True,
            "session resolved, worktree exists and process is not alive",
        ),
    ],
)
def test_resumability_joins_session_tree_and_process_state(
    isolated_reckon_home: Path,
    repository: Path,
    worktree_exists: bool,
    process_alive: bool,
    resumable: bool,
    reason: str,
) -> None:
    _write_pointer(
        isolated_reckon_home,
        repository,
        "run-eligibility",
        session_id="session-pointer",
        worktree_exists=worktree_exists,
        process_alive=process_alive,
    )

    row = _only_row(repository)

    assert row["resumable"] is resumable
    assert row["resumable_reason"] == reason
