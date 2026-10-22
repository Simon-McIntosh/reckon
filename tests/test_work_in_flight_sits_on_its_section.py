"""Work in flight sits on its section, with the one run classification.

The roadmap has always reported live runs against the plan a run targets, so a
reader looking for the work somebody is doing on a section could not find it:
a section row said what was ready and nothing about the run sitting on it. The
rows here are the runs the plan-level partition already classified, grouped
under the section each one names, carrying the same classification ``crew live``
gives them and the write paths they hold.

The three readings this file holds are the three a section row has to make: the
run and the classification on it, a hold only a run can carry (a manifest that
reports blocked), and a hold that belongs to two runs at once (one path claimed
by a run holding it and a run waiting on it).

Every fixture is written into a synthetic docs tree under the test's own
``tmp_path``, so no test reads a mounted project's state.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import recovery
from reckon.crew.node import claim_disposition
from reckon.mcp_views import partition_live_runs
from reckon.roadmap import build_roadmap
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "sample"
CLAIMED_PATH = "pkg/one.py"


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(tmp_path / "repo" / "docs")}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()
    return docs


def _plan(slug: str = "alpha") -> dict[str, Any]:
    return {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": "active",
        "impl": 0.3,
        "depends_on": [],
        "effort": "S",
        "roi": "high",
        "blocking": [],
        "gates": [],
        "followups": [],
    }


def _sprint(sprint_id: str, *slugs: str) -> dict[str, Any]:
    return {"id": sprint_id, "status": "active", "items": list(slugs)}


def _pointer(
    *,
    run_id: str = "run-live",
    section: str = "s1",
    plan: str = "alpha",
    phase: str = "working",
    repo: Path | None = None,
    write_paths: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One live pointer, as the fleet writes it for a dispatched node."""

    node: dict[str, Any] = {"id": f"{plan}-{section}", "plan": plan, "section": section}
    if write_paths is not None:
        node["write_paths"] = list(write_paths)
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-one",
        "created_at": "2026-10-01T00:00:00Z",
        "phase": phase,
        "process_alive": True,
        # Liveness is carried rather than re-derived: the pointer names a host
        # that is not the reading host, as a shared crew home produces.
        "launcher_host": "another-login-node",
        "node": node,
    }
    if repo is not None:
        record["repo"] = str(repo)
    record.update(extra)
    return record


def _dead_pid() -> int:
    """A pid that has certainly exited, for a pointer recording a gone worker.

    The fence reads a pointer's liveness from its recorded pid and not from a
    ``process_alive`` field, so a fixture that only sets the field reads as a
    run whose worker has not been spawned yet. Reaping a child gives a pid the
    probe on this host answers ``False`` for.
    """

    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def _section_rows(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row["ref"]): row for row in report["immediate_roadmap"] if "ref" in row}


def _build(docs: Path, pointers: list[dict[str, Any]], monkeypatch) -> dict[str, Any]:
    # ``reckon.crew`` is the facade the concern modules read their overrides
    # through, so one patch reaches the live-pointer read the roadmap makes and
    # the claim registry the held paths come from, and both see one fleet.
    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: list(pointers))
    return build_roadmap(
        PROJECT,
        [_plan()],
        [_sprint("S1", "alpha")],
        docs_dir=docs,
    )


def test_a_live_run_sits_on_its_section_row_with_its_classification(
    mounted_project, monkeypatch
):
    """The run appears on its section's row with the classification the live
    partition reports for it, so a section row and ``crew live`` cannot
    disagree. A row that derived its own classification instead of carrying the
    partition's fails here, which the pointer classifier's own answer would not
    catch."""

    pointer = _pointer()
    live, _interrupted = partition_live_runs(PROJECT, [pointer])
    report = _build(mounted_project, [pointer], monkeypatch)

    row = _section_rows(report)["alpha#s1"]
    runs = row["runs"]

    assert [run["run_id"] for run in runs] == ["run-live"]
    assert runs[0]["classification"] == live["alpha"][0]["classification"]
    assert row["plan"] == "alpha"


def test_a_blocked_manifest_makes_its_section_run_blocked(
    mounted_project, monkeypatch, tmp_path
):
    """A run whose manifest reports blocked holds its section, by that kind and
    with the text that stopped it."""

    manifest = tmp_path / "manifest.md"
    log = tmp_path / "log.jsonl"
    now = time.time()
    manifest.write_text(
        "node: alpha-s1\n"
        "status: blocked\n"
        "commits: HEAD\n"
        "blockers:\n"
        '  - "waiting on the coil supply model to land"\n'
    )
    log.write_text('{"type":"turn.started"}\n')
    os.utime(manifest, (now - 10, now - 10))
    os.utime(log, (now, now))
    pointer = _pointer(
        process_alive=False,
        manifest_path=str(manifest),
        log_path=str(log),
    )
    # The fixture is only the input is meaningful if the classifier agrees on it.
    assert recovery.classify_pointer(pointer)["classification"] == "blocked"

    report = _build(mounted_project, [pointer], monkeypatch)

    row = _section_rows(report)["alpha#s1"]
    blocked = [hold for hold in row["blockers"] if hold["kind"] == "run-blocked"]

    assert [hold["run_id"] for hold in blocked] == ["run-live"]
    assert "coil supply model" in blocked[0]["blocker"]


def test_two_runs_over_one_path_report_the_holder_then_the_queued_run(
    mounted_project, monkeypatch, tmp_path
):
    """Two runs claiming one path are a holder and a run waiting on it, and the
    waiting run's section names both, holder first."""

    repo = tmp_path / "repo"
    holder = _pointer(
        run_id="run-holder",
        section="s1",
        repo=repo,
        write_paths=[CLAIMED_PATH],
    )
    queued = _pointer(
        run_id="run-queued",
        section="s2",
        phase="queued",
        process_alive=None,
        repo=repo,
        write_paths=[CLAIMED_PATH],
    )
    report = _build(mounted_project, [holder, queued], monkeypatch)

    rows = _section_rows(report)
    held = [
        hold for hold in rows["alpha#s2"]["blockers"] if hold["kind"] == "held-path"
    ]

    assert held, "the waiting section names the path it waits on"
    assert [hold["path"] for hold in held] == [CLAIMED_PATH]
    assert held[0]["holder"] == "run-holder"
    assert held[0]["waiting"] == "run-queued"
    # The holder's own section waits on nothing: the pair is reported once, on
    # the row of the run that cannot start.
    assert [
        hold for hold in rows["alpha#s1"]["blockers"] if hold["kind"] == "held-path"
    ] == []


def test_a_stopped_run_that_left_nothing_releases_its_path(
    mounted_project, monkeypatch, tmp_path
):
    """A stopped run with no unintegrated work holds nothing, so the run queued
    behind the same path waits on nobody and no section row names either.

    The fence stops a claim binding for two reasons and only one of them means a
    run is waiting: this is the other one, where the path is free and the queued
    run is held by nothing."""

    repo = tmp_path / "repo"
    # ``process_alive`` is carried for the display; the fence reads the pid, so
    # the fixture records one whose process has exited and no worktree, which is
    # the released condition rather than a claim still holding work.
    stopped = _pointer(
        run_id="run-stopped",
        section="s1",
        phase="completed",
        pid=_dead_pid(),
        process_alive=False,
        repo=repo,
        write_paths=[CLAIMED_PATH],
    )
    queued = _pointer(
        run_id="run-queued",
        section="s2",
        phase="queued",
        process_alive=None,
        repo=repo,
        write_paths=[CLAIMED_PATH],
    )
    # The fixture means what this test reads it as only if the fence agrees, so
    # the stopped run's own claim is disregarded before anything is asserted
    # about the row — and disregarded for the released reason, since a claim
    # that stopped and left unintegrated work binds and would hold the path.
    disposition = claim_disposition(stopped)
    assert disposition.binding is False
    assert "left no process and no unintegrated work" in disposition.reason

    report = _build(mounted_project, [stopped, queued], monkeypatch)

    held = [
        hold
        for row in _section_rows(report).values()
        for hold in row["blockers"]
        if hold["kind"] == "held-path"
    ]

    assert held == []


def test_a_plan_with_no_runs_reports_no_section_row_for_an_undeclared_section(
    mounted_project, monkeypatch
):
    """A section nothing declares and no run names is not invented as a row, so
    the rows a reader sees are the work that exists."""

    report = _build(mounted_project, [], monkeypatch)

    assert _section_rows(report) == {}
