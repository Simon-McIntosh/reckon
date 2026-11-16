"""Measure the station's tick against a crew home populated with live runs.

The station completes a tick under 30 seconds and holds under 2 GB at the
fleet's target scale of about 230 live runs, or the monitor and its obligations
fall behind the fleet they describe. This measure builds a temporary crew home
carrying that many live runs across mounted projects, runs consecutive ticks
against it, and records each tick's duration and the station's resident memory
after it, so the bound is read from the instrument rather than assumed.

It writes JSON to stdout, and to --out when that is named, then exits non-zero
when any tick reached the duration bound or any reading reached the memory
bound, so a caller can use the exit status as the gate.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reckon.crew import station, station_store

PROJECTS = ("alpha", "beta", "gamma", "delta")
TICK_SECONDS = 30.0
MEMORY_BYTES = 2 * 1024**3
STREAM_LINES = 40
ROW_BYTES = 2048

PLAN_HTML = (
    "<!doctype html><html><head>"
    '<meta name="docs-project" content="{project}">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="fixture">'
    '<meta name="plan-title" content="Fixture">'
    '<meta name="plan-status" content="active">'
    '<meta name="plan-version" content="3">'
    '</head><body><h2 id="delivery">Delivery</h2>'
    "<p>Extend the existing mechanism.</p></body></html>"
)


def resident_bytes() -> int:
    """The current process's resident set size, read from its own status."""
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    raise RuntimeError("this kernel does not report VmRSS")


def seed_projects(root: Path, home: Path) -> dict[str, Path]:
    """Create one mounted project per name, each a real repository."""
    mounts: dict[str, Path] = {}
    for project in PROJECTS:
        repo = root / project
        plans = repo / "docs" / "plans"
        plans.mkdir(parents=True)
        (plans / "fixture.html").write_text(
            PLAN_HTML.replace("{project}", project), encoding="utf-8"
        )
        for args in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "measure@example.invalid"],
            ["config", "user.name", "Measure"],
            ["add", "docs/plans/fixture.html"],
            ["commit", "-q", "-m", "chore: seed fixture", "-m", "A committed plan."],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        mounts[project] = repo / "docs"
    (home / "mounts.json").write_text(
        json.dumps({name: str(path) for name, path in mounts.items()}),
        encoding="utf-8",
    )
    return mounts


def append_rows(path: Path, rows: int, *, at: str, first: int = 0) -> None:
    """Append ``rows`` stream records to one run's stream."""
    body = "".join(
        json.dumps(
            {
                "type": "assistant",
                "index": first + line,
                "text": "x" * ROW_BYTES,
                "ts": at,
            }
        )
        + "\n"
        for line in range(rows)
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(body)


def populate(home: Path, runs: int, stream_bytes: bytes) -> list[Path]:
    """Write one live pointer and one stream per run, spread across projects."""
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    created = (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat()
    rows = max(1, stream_bytes // ROW_BYTES)
    streams: list[Path] = []
    for index in range(runs):
        run_id = f"r-measure-{index:03}"
        project = PROJECTS[index % len(PROJECTS)]
        log_path = home / "crew" / "runs" / run_id / "stream.jsonl"
        log_path.parent.mkdir(parents=True)
        append_rows(log_path, rows, at=created)
        streams.append(log_path)
        (live / f"{run_id}.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project": project,
                    "session": f"session-{index % 17}",
                    "phase": "working",
                    "created_at": created,
                    "launcher_host": socket.gethostname(),
                    "node": {"id": run_id, "plan": "fixture", "section": "delivery"},
                    "reason": "running",
                    "log_path": str(log_path),
                }
            ),
            encoding="utf-8",
        )
    return streams


def measure(
    home: Path,
    scratch: Path,
    mounts: dict[str, Path],
    ticks: int,
    label: str,
    streams: list[Path],
    arrival_bytes: bytes,
    *,
    tick_seconds: float = TICK_SECONDS,
    memory_bytes: int = MEMORY_BYTES,
) -> dict:
    """Run consecutive ticks and record each duration and the memory after it."""
    store = station_store.StationStore(scratch / "runtime")
    node = station.Station(store, home=home / "crew", projects=lambda: dict(mounts))
    moment = datetime.now(tz=UTC)
    arriving = max(1, arrival_bytes // ROW_BYTES) if arrival_bytes else 0
    readings = []
    try:
        for index in range(ticks):
            if index and arriving:
                at = datetime.now(tz=UTC).isoformat()
                for stream in streams:
                    append_rows(stream, arriving, at=at, first=index * arriving)
            started = time.monotonic()
            node.tick(now=moment + timedelta(seconds=5 * index))
            readings.append(
                {
                    "tick": index,
                    "seconds": round(time.monotonic() - started, 3),
                    "resident_bytes": resident_bytes(),
                }
            )
    finally:
        node.duties.drain(timeout=30)
        node.reflex.stop()
        store.close()
    return {
        "label": label,
        "module": station.__file__,
        "cwd": os.getcwd(),
        "python": sys.executable,
        "runs": len(streams),
        "stream_bytes": sum(stream.stat().st_size for stream in streams),
        "arrival_bytes": arrival_bytes,
        "ticks": readings,
        "tick_bound_seconds": tick_seconds,
        "memory_bound_bytes": memory_bytes,
        "max_tick_seconds": max(row["seconds"] for row in readings),
        "max_resident_bytes": max(row["resident_bytes"] for row in readings),
        "over_bound": [
            row
            for row in readings
            if row["seconds"] >= tick_seconds or row["resident_bytes"] >= memory_bytes
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=230)
    parser.add_argument("--ticks", type=int, default=20)
    parser.add_argument("--out", default=None)
    parser.add_argument("--label", default="head")
    parser.add_argument("--stream-bytes", type=int, default=256 * 1024)
    parser.add_argument("--arrival-bytes", type=int, default=8 * 1024)
    parser.add_argument("--tick-bound-seconds", type=float, default=TICK_SECONDS)
    parser.add_argument("--memory-bound-bytes", type=int, default=MEMORY_BYTES)
    args = parser.parse_args(argv)

    scratch = Path(
        tempfile.mkdtemp(dir=os.environ.get("TMPDIR"), prefix="tick-measure-")
    )
    try:
        home = scratch / "config"
        home.mkdir()
        os.environ["RECKON_HOME"] = str(home)
        os.environ["RECKON_MOUNTS_PATH"] = str(home / "mounts.json")
        os.environ["XDG_RUNTIME_DIR"] = str(scratch / "runtime")
        (scratch / "runtime").mkdir()
        mounts = seed_projects(scratch / "projects", home)
        streams = populate(home, args.runs, args.stream_bytes)
        result = measure(
            home,
            scratch,
            mounts,
            args.ticks,
            args.label,
            streams,
            args.arrival_bytes,
            tick_seconds=args.tick_bound_seconds,
            memory_bytes=args.memory_bound_bytes,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(
        f"max_tick_seconds={result['max_tick_seconds']} "
        f"max_resident_mb={result['max_resident_bytes'] // 2**20} "
        f"over_bound={len(result['over_bound'])}"
    )
    return 1 if result["over_bound"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
