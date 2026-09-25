"""A sprint's liveness comes from the crews working it, and from nothing else.

The fixture is a synthetic config home and docs tree: two plans in two sprints
with a live pointer each (one sprint also holds a blocked pointer) and a third
plan in a third sprint whose only pointer has reached a terminal status. Liveness
must follow the pointers, and the read must leave every file it touches alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reckon import sprint_liveness as sl
from reckon.crew.runs import list_live
from reckon.project_state import create_project_state, write_resource
from reckon.resources import read_sprint_record

# A project name no test elsewhere uses, so the fleet watch-stream registry and
# the live-pointer reader cannot pick up a peer's state.
PROJECT = "liveness-sample"


def _write_plan(docs_dir: Path, slug: str, sprint: str) -> Path:
    path = docs_dir / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-sprint" content="{sprint}">'
        "</head><body></body></html>",
        encoding="utf-8",
    )
    return path


def _write_manifest(home: Path, run_id: str, status: str) -> Path:
    manifest = home / "crew" / "runs" / run_id / "manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"status: {status}\n", encoding="utf-8")
    return manifest


def _write_pointer(
    home: Path,
    run_id: str,
    plan: str,
    session: str,
    *,
    phase: str,
    alive: bool,
    manifest_status: str | None = None,
) -> dict:
    """Write one live pointer, its stream and (optionally) its manifest."""
    run_dir = home / "crew" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    stream = run_dir / "stdout.jsonl"
    stream.write_text('{"type":"thread.started"}\n', encoding="utf-8")

    record: dict = {
        "run_id": run_id,
        "project": PROJECT,
        "session": session,
        "phase": phase,
        "process_alive": alive,
        "node": {"plan": plan, "section": "s2"},
        "log_path": str(stream),
    }
    if manifest_status is not None:
        record["manifest_path"] = str(_write_manifest(home, run_id, manifest_status))
    live_dir = home / "crew" / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return record


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "config-home"
    docs = tmp_path / "repo" / "docs"
    return home, docs


def _seed(home: Path, docs: Path) -> None:
    _write_plan(docs, "plan-alpha", "S100")
    _write_plan(docs, "plan-beta", "S101")
    _write_plan(docs, "plan-gamma", "S102")

    # S100: a working crew, plus a blocked one on the same sprint. The blocked
    # pointer must not count as live and must not inflate the live run count.
    _write_pointer(home, "run-a1", "plan-alpha", "s-a1", phase="working", alive=True)
    _write_pointer(
        home,
        "run-a2",
        "plan-alpha",
        "s-a2",
        phase="working",
        alive=False,
        manifest_status="blocked",
    )
    # S101: a single working crew.
    _write_pointer(home, "run-b1", "plan-beta", "s-b1", phase="working", alive=True)
    # S102: a terminal pointer, nothing live.
    _write_pointer(
        home,
        "run-c1",
        "plan-gamma",
        "s-c1",
        phase="failed",
        alive=False,
        manifest_status="failed",
    )


def _store_sprint(docs: Path, sprint_id: str, status: str) -> None:
    """Publish one sprint resource through the project's own sprint writer.

    A stored status is the field a reader trusts at its peril: it is present and
    readable by the project's own reader, so a control that seeds one is testing a
    marker that disagrees with the pointers rather than an absent input that
    happens to default to some value.
    """
    create_project_state(docs, PROJECT)
    write_resource(
        docs,
        PROJECT,
        "sprint",
        sprint_id,
        {"status": status, "theme": f"{sprint_id} stored marker"},
        0,
        create=True,
    )


def _mtimes(*roots: Path) -> dict[str, int]:
    """Every file's mtime under the given roots, keyed by path."""
    seen: dict[str, int] = {}
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                seen[str(path)] = path.stat().st_mtime_ns
    return seen


def test_liveness_follows_the_pointers_and_changes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, docs = _fixture(tmp_path)
    _seed(home, docs)
    monkeypatch.setenv("RECKON_HOME", str(home))

    before = _mtimes(home, docs)
    assert before, "the fixture wrote no files to watch"

    result = sl.sprint_liveness(PROJECT, docs, list_live(project=PROJECT))

    after = _mtimes(home, docs)
    assert after == before, "the read changed a file's mtime"

    assert set(result) == {"S100", "S101", "S102"}
    assert [sid for sid, row in result.items() if row["live"]] == ["S100", "S101"]

    assert result["S100"]["live"] is True
    assert result["S100"]["live_runs"] == ["run-a1"]
    assert result["S100"]["live_sessions"] == ["s-a1"]
    assert result["S100"]["held_runs"] == 1
    assert result["S100"]["last_activity_at"] is not None

    assert result["S101"]["live"] is True
    assert result["S101"]["live_runs"] == ["run-b1"]
    assert result["S101"]["live_sessions"] == ["s-b1"]
    assert result["S101"]["held_runs"] == 0

    assert result["S102"]["live"] is False
    assert result["S102"]["live_runs"] == []
    assert result["S102"]["live_sessions"] == []
    assert result["S102"]["held_runs"] == 0


def test_the_default_pointer_read_finds_the_synthetic_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting the record set composes list_live over the config home."""
    home, docs = _fixture(tmp_path)
    _seed(home, docs)
    monkeypatch.setenv("RECKON_HOME", str(home))

    explicit = sl.sprint_liveness(PROJECT, docs, list_live(project=PROJECT))
    defaulted = sl.sprint_liveness(PROJECT, docs)

    assert defaulted == explicit
    assert [sid for sid, row in defaulted.items() if row["live"]] == ["S100", "S101"]


def test_a_stored_active_sprint_status_does_not_make_it_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored sprint marker is not liveness, however active it reads.

    The third sprint holds nothing but a terminal pointer, so no pointer on it is
    live. Its stored status — published through the project's own sprint writer —
    genuinely reads active. Liveness must still be false: a stored marker is a
    scheduling record, and trusting it is the stale-marker defect this read
    exists to remove. Without this case the gate cannot tell a pointers-only read
    from one that ORs the stored marker into the verdict, because no sprint in
    the fixture carried a stored status to disagree with its pointers.
    """
    home, docs = _fixture(tmp_path)
    _seed(home, docs)
    monkeypatch.setenv("RECKON_HOME", str(home))
    _store_sprint(docs, "S102", "active")

    # The premise the assertion rests on: the marker is present and readable by
    # the project's own reader, not an absent field defaulting to a value.
    assert read_sprint_record(docs, PROJECT, "S102").get("status") == "active"

    before = _mtimes(home, docs)
    result = sl.sprint_liveness(PROJECT, docs, list_live(project=PROJECT))
    after = _mtimes(home, docs)

    assert after == before, "the read changed a file's mtime"
    assert result["S102"]["live"] is False
    assert [sid for sid in result if result[sid]["live"]] == ["S100", "S101"]
