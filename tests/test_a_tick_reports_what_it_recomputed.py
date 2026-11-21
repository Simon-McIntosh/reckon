"""A finished tick and sweep pass report the answers each one recomputed.

The station's reading says how much work a pass paid for. Without it a quiet
pass and one that re-parsed the fleet read identically, so a monitor could only
guess whether its cost was the bytes that moved or the whole corpus.

Three rounds are driven against a crew home holding twenty live pointers,
twenty worker streams and twenty plan files, with one sweep pass after each
tick. The first round is cold and must report a recompute for every memo kind
it used; the second changes nothing and must report no full recompute and no
stream bytes read; between the second and the third one plan file is rewritten
and one stream gains four kilobytes, and the third must report that single
incremental extension and the plan's own recompute, and nothing else.

The fixture's first run is finished — its pointer carries a terminal phase and
an exit record — so the pass that observes its stream is the sweep's own
reader rather than a liveness scan, which keeps one stream's growth attributed
to one pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import _backends, file_memo
from reckon.crew import obligation_snapshot as snapshots
from reckon.crew import recovery_wait, resumption, station, station_store

PROJECT = "fixture"
SESSION = "session-fixture"
RUNS = 20
PLANS = 20
FINISHED_RUN = 0
CHANGED_PLAN = "fixture-01"
EXTENSION_BYTES = 4096
QUIET_ROW_BYTES = 128


def _plan_html(slug: str) -> str:
    return (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-slug" content="{slug}">'
        f'<meta name="plan-title" content="Fixture {slug}">'
        '<meta name="plan-status" content="shipped">'
        '<meta name="plan-version" content="3">'
        '<meta name="plan-impl" content="1.0">'
        "</head><body><h2 id=delivery>Delivery</h2>"
        "<p>Extend the existing mechanism.</p></body></html>"
    )


def _stream_body(count: int) -> str:
    return "".join(
        json.dumps(
            {
                "type": "assistant",
                "index": index,
                "text": "x" * QUIET_ROW_BYTES,
                "ts": "seed",
            }
        )
        + "\n"
        for index in range(count)
    )


def _growth_line() -> str:
    """One stream record of exactly the extension the third round reports."""
    empty = len(json.dumps({"type": "user", "text": ""})) + 1
    return json.dumps({"type": "user", "text": "z" * (EXTENSION_BYTES - empty)}) + "\n"


def _seed_project(scratch: Path, home: Path, edited: float) -> Path:
    repo = scratch / "projects" / PROJECT
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    for index in range(PLANS):
        path = plans / f"fixture-{index:02d}.html"
        path.write_text(_plan_html(f"fixture-{index:02d}"), encoding="utf-8")
        os.utime(path, (edited, edited))
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "user.name", "Fixture"],
        ["add", "docs/plans"],
        ["commit", "-m", "chore: seed the plan fixture", "-m", "Twenty shipped plans."],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo / "docs" / "plans"


def _seed_fleet(home: Path, created: str) -> list[Path]:
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    streams: list[Path] = []
    for index in range(RUNS):
        run_id = f"r-fixture-{index:03d}"
        directory = home / "crew" / "runs" / run_id
        directory.mkdir(parents=True)
        stream = directory / "stream.jsonl"
        stream.write_text(_stream_body(8), encoding="utf-8")
        streams.append(stream)
        pointer = {
            "run_id": run_id,
            "project": PROJECT,
            "session": SESSION,
            "phase": "working",
            "created_at": created,
            "launcher_host": socket.gethostname(),
            "backend": "codex",
            "launch": "cli",
            "node": {"id": run_id, "plan": f"fixture-{index:02d}"},
            "reason": "running",
            "log_path": str(stream),
        }
        if index == FINISHED_RUN:
            pointer["phase"] = "complete"
            (directory / "exit.json").write_text(
                json.dumps({"run_id": run_id, "exit_code": 0, "exited_at": created}),
                encoding="utf-8",
            )
        (live / f"{run_id}.json").write_text(json.dumps(pointer), encoding="utf-8")
    return streams


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "config"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    plans = _seed_project(tmp_path, home, time.time() - 3600)
    created = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    streams = _seed_fleet(home, created)

    _cold()

    def sweep(project, *, session=None, now=None, writer=None):
        return resumption.sweep(project, config={}, now=now, writer=writer)

    beat = station.SweepBeat(sweep=sweep, beat_seconds=0.0)
    beat._start = lambda run: run()
    node = station.Station(
        station_store.StationStore(runtime),
        home=home / "crew",
        projects=lambda: {PROJECT: plans.parent},
        duties=SimpleNamespace(
            committing=contextlib.nullcontext, request=lambda *a, **k: None
        ),
        sweeps=beat,
    )
    # Each round drives exactly one pass itself. A tick requesting its own
    # passes would overwrite the reading under test with a later pass, so the
    # rounds own the sweeps for the length of the fixture.
    node.sweeps = SimpleNamespace(request=lambda *a, **k: None, waiting=list)
    node.reflex.stop()
    try:
        yield SimpleNamespace(node=node, beat=beat, plans=plans, streams=streams)
    finally:
        node.reflex.stop()
        node.store.close()


def _cold() -> None:
    """Empty every cache a pass reads through, so the next pass is a first read."""
    file_memo.clear()
    _backends._PARSED_STREAMS.clear()
    _backends._STREAM_SUMMARIES.clear()
    recovery_wait._RUN_STREAM_NAMES.clear()


def _round(fleet, moment: datetime) -> dict[str, dict[str, dict[str, int]]]:
    fleet.node.tick(now=moment)
    fleet.beat.request(PROJECT, SESSION, moment=moment, changed=True)
    reading = snapshots.read_station_tick(now=moment)
    assert reading is not None, "the station store answered no reading"
    tick = (reading.get("last_tick") or {}).get("memo_counts")
    sweep = (reading.get("last_sweep") or {}).get("memo_counts")
    assert isinstance(tick, dict), "the finished tick reported no memo counts"
    assert isinstance(sweep, dict), "the finished sweep pass reported no memo counts"
    return {"tick": tick, "sweep": sweep}


def _recomputed(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {kind: row for kind, row in counts.items() if row.get("recomputed")}


def _read(counts: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    return {kind: row for kind, row in counts.items() if row["bytes_read"]}


def _extensions(rounds: dict[str, dict[str, dict[str, int]]]):
    return [
        (pass_name, kind, row)
        for pass_name, counts in rounds.items()
        for kind, row in counts.items()
        if row.get("extended")
    ]


def test_each_pass_reports_what_it_recomputed(fleet) -> None:
    # Building the station reads some of the same files a pass reads. Emptying
    # the caches here makes the first round a genuine first read.
    _cold()
    moment = datetime.now(tz=UTC)

    cold = _round(fleet, moment)
    for pass_name, counts in cold.items():
        assert counts, f"the first round's {pass_name} reported no counts at all"
    # A kind the tick has already answered is one the sweep is rightly served
    # from cache, so coldness is a property of the round rather than of one
    # pass: the pair must have recomputed every kind either of them used.
    cold_total: dict[str, int] = {}
    for counts in cold.values():
        for kind, row in counts.items():
            cold_total[kind] = cold_total.get(kind, 0) + row["recomputed"]
    served = {kind: count for kind, count in cold_total.items() if count < 1}
    assert not served, (
        f"the first round used these kinds without recomputing any of them: {served}"
    )

    quiet = _round(fleet, moment + timedelta(seconds=5))
    for pass_name, counts in quiet.items():
        recomputed = _recomputed(counts)
        assert not recomputed, (
            f"the quiet round's {pass_name} recomputed these kinds: {recomputed}"
        )
        read = _read(counts)
        assert not read, f"the quiet round's {pass_name} read these bytes: {read}"

    plan = fleet.plans / f"{CHANGED_PLAN}.html"
    rewritten = _plan_html(CHANGED_PLAN) + "<!-- an edit landed -->"
    plan.write_text(rewritten, encoding="utf-8")
    restamped = time.time() - 3600
    os.utime(plan, (restamped, restamped))
    growth = _growth_line()
    assert len(growth.encode("utf-8")) == EXTENSION_BYTES
    with fleet.streams[FINISHED_RUN].open("a", encoding="utf-8") as handle:
        handle.write(growth)

    moved = _round(fleet, moment + timedelta(seconds=10))

    extensions = _extensions(moved)
    assert len(extensions) == 1, f"extended answers: {extensions}"
    pass_name, kind, row = extensions[0]
    assert row["extended"] == 1 and row["bytes_read"] == EXTENSION_BYTES, (
        f"the {pass_name}'s {kind} extension was {row}"
    )
    assert kind == "stream_observation", (
        f"the incremental extension was reported under {kind}"
    )

    plan_bytes = len(rewritten.encode("utf-8"))
    recomputed = _recomputed(moved["sweep"])
    assert "parse_meta" in recomputed, (
        f"the sweep read the rewritten plan but reported no parse: {recomputed}"
    )
    # A plan is answered through more than one memo kind and each re-read the
    # same file. Each of those kinds must have recomputed the plan exactly
    # once, and no other kind may appear at all: a count of one settles that
    # nothing outside the rewritten plan was re-read, which the bytes alone
    # would not.
    plan_kinds = {"parse_meta", "plan_text"}
    assert set(recomputed) <= plan_kinds, (
        f"the third round recomputed kinds outside the plan: {recomputed}"
    )
    for kind, row in recomputed.items():
        assert row["recomputed"] == 1, (
            f"the third round recomputed {kind} {row['recomputed']} times: {row}"
        )
        assert row["bytes_read"] == plan_bytes, (
            f"the {kind} recompute did not read the rewritten plan: {row}"
        )
    assert not _recomputed(moved["tick"]), (
        f"the third round's tick recomputed: {_recomputed(moved['tick'])}"
    )
