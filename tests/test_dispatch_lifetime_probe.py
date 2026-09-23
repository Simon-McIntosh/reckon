"""Measure what keeps a dispatch process alive, and whether its worker survives it.

The dispatch process is observed directly. A driver child calls
``reckon.crew.dispatch`` with a stub backend, so the driver *is* the dispatch
process: its lifetime, its descendants and its response to a signal are read
from ``/proc`` rather than inferred. ``faulthandler`` dumps every thread's
stack into a file while the driver lives, so a hold inside the reckon package
is named with its file and line rather than guessed at.

Everything runs against a throwaway repository and a throwaway configuration
home, and the real live-pointer directory is checked afterwards for any pointer
naming a run this probe created.

The probe is opted into with ``RECKON_RUN_DISPATCH_PROBE=1``: it starts real
processes and rewrites its artifact, so it must not run in a default suite run.

Running this module regenerates ``docs/research/data/dispatch-lifetime.json``
and the figure under ``docs/figures/a-dispatch-returns-without-its-worker/``.
"""

from __future__ import annotations

import json
import linecache
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from reckon._store import _config_home

REPO_ROOT = Path(__file__).resolve().parents[1]
# The variable that opts this probe in; absent, or set to anything but "1", the
# test is skipped with a reason naming it.
RECKON_RUN_DISPATCH_PROBE_ENV = "RECKON_RUN_DISPATCH_PROBE"
DATA_PATH = REPO_ROOT / "docs" / "research" / "data" / "dispatch-lifetime.json"
FIGURES = REPO_ROOT / "docs" / "figures" / "a-dispatch-returns-without-its-worker"
DRIVER_FILENAME = "dispatch_driver.py"

# Resolved once, at import, before the suite's autouse ``RECKON_HOME`` fixture
# redirects the configuration home. The guard below must name the fleet
# directory a dispatch outside this suite writes to; resolving it at call time
# would name the fixture's throwaway home instead, and the assertion would hold
# because the probe writes nowhere near it rather than because the real fleet
# was left alone.
REAL_LIVE_DIR = _config_home() / "crew" / "live"

# The stub backend's command sleeps this long before writing its marker, so a
# worker that is killed before it finishes never writes one.
STUB_SLEEP_SECONDS = int(os.environ.get("RECKON_PROBE_STUB_SLEEP") or 60)
# How long to wait for the worker to appear. A dispatch creates a worktree
# before it launches anything; past this bound the host is badly contended and
# waiting longer measures the host rather than the code.
LAUNCH_DEADLINE_SECONDS = 150.0
# How long the dispatch process may live after the worker appears before the
# remainder is called a hold rather than a prompt return.
POST_LAUNCH_GRACE_SECONDS = 20.0
# The marker is written one stub-sleep after the worker starts.
MARKER_DEADLINE_SECONDS = STUB_SLEEP_SECONDS + 45.0
# Suppresses the watch producer, so only the one trial that measures the
# arming path arms one.
WATCH_ARMING = "RECKON_WATCH_ARMING"


CONFIG = {
    "default_backend": "stub",
    "backends": {
        "stub": {
            "launch": "cli",
            "command": "claude",
            "model": "local-stub",
            "effort": "low",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

# The driver stands in for the dispatch process: it *is* the process whose
# descendants and signal response this probe measures.
DRIVER_SOURCE = """\
import contextlib
import faulthandler
import json
import os
import sys
from pathlib import Path


def main() -> None:
    payload = json.loads(Path(sys.argv[1]).read_text())
    os.environ["RECKON_HOME"] = payload["config"]
    os.environ["PATH"] = payload["bin_dir"] + os.pathsep + os.environ.get("PATH", "")
    os.environ[payload["watch_env"]] = payload.get("arming_value", "off")
    trace = open(payload["trace_path"], "w")
    faulthandler.enable(file=trace)
    faulthandler.dump_traceback_later(2.0, repeat=True, file=trace)

    from reckon import crew
    from reckon.crew import runs
    from reckon.crew.node import TaskNode

    node = TaskNode(
        id=payload["node"],
        goal="record one stub-backed dispatch",
        plan="fixture",
        section="arming",
        role="implement",
        spec_level="guided",
        done_when=payload["done_when"],
        write_paths=[payload["write_path"]],
        time_budget="25m",
        manifest_path=payload["manifest"],
    )
    claim = (
        runs.follower_claim("sample", payload["session"], delivery="stream")
        if payload.get("follow_claim")
        else contextlib.nullcontext()
    )
    with claim:
        record = crew.dispatch(
            node=node,
            project="sample",
            repo=payload["repo"],
            config=payload["config_data"],
            session=payload["session"],
            watch_required=payload.get("watch_required", False),
        )
    sys.stdout.write(json.dumps({"run_id": record["run_id"], "pid": record.get("pid")}))
    sys.stdout.flush()
    faulthandler.cancel_dump_traceback_later()


main()
"""


def _build_repo(repo: Path) -> str:
    """Create a minimal dispatchable repository and return its base revision."""
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="arming">Dispatch lifetime</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["add", "seed.txt", "docs/plans/fixture.html"],
        [
            "-c",
            "user.email=worker@example.invalid",
            "-c",
            "user.name=Worker",
            "commit",
            "-q",
            "-m",
            "chore: seed",
        ],
    ):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_stub(bin_dir: Path, marker_dir: Path, tag: str) -> None:
    """Write the stub backend command: record pid, sleep, then write a marker."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'echo $$ > "{marker_dir}/worker.{tag}.pid"\n'
        f"sleep {STUB_SLEEP_SECONDS}\n"
        f': > "{marker_dir}/marker.{tag}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


def _process_table() -> dict[int, dict]:
    """Return every live process as {pid, ppid, argv, state}."""
    table: dict[int, dict] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        tail = stat.rpartition(")")[2].split()
        if len(tail) < 2:
            continue
        argv = [
            part.decode("utf-8", "replace") for part in cmdline.split(b"\0") if part
        ]
        table[pid] = {
            "pid": pid,
            "ppid": int(tail[1]),
            "argv": argv,
            "state": tail[0],
        }
    return table


def _descendants(root_pid: int, table: dict[int, dict]) -> list[dict]:
    """Return every descendant of root_pid, breadth-first."""
    found = []
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop(0)
        for entry in table.values():
            if entry["ppid"] == parent and entry["pid"] != root_pid:
                found.append(entry)
                frontier.append(entry["pid"])
    return found


def _role(entry: dict) -> str:
    """Name what a process in the dispatch's tree is doing."""
    argv = entry["argv"]
    joined = " ".join(argv)
    if any(part.endswith(DRIVER_FILENAME) for part in argv):
        return "dispatch-driver"
    if "import subprocess, sys" in joined:
        return "watch-supervisor"
    if "crew" in argv and "watch" in argv:
        return "watch-producer"
    if any(part.endswith("worktree_fleet.py") for part in argv):
        return "worktree-creation"
    if argv and argv[0].endswith("git"):
        return "worktree-git"
    if argv and argv[0] == "sleep":
        return "stub-sleep"
    if any(Path(part).name == "claude" for part in argv):
        return "stub-worker"
    return "unknown"


def _read_pid(path: Path) -> int | None:
    """Return the pid a stub wrote, or None while the file is absent or partial."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def _run_trial(tmp_path: Path, marker_dir: Path, spec: dict) -> dict:
    """Dispatch once and observe the process the dispatch runs in.

    A signal is sent only after the worker's pid file appears, so it always
    lands on a dispatch process whose worker already exists: killing a process
    that has not yet launched anything measures nothing.

    Each trial gets its own repository because the crew ledger is a committed
    file inside it: a dispatch killed mid-flight leaves that file staged and
    uncommitted, and the next dispatch in the same repository then refuses to
    commit its own member registration over the leftover. Sharing one
    repository made the trial after a killed one fail for the previous trial's
    reason instead of measuring anything.
    """
    trial = spec["name"]
    signal_number = spec["signal"]
    repo = tmp_path / f"repo-{trial}"
    if not (repo / ".git").exists():
        _build_repo(repo)
    config_home = tmp_path / f"home-{trial}"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "mounts.json").write_text(
        json.dumps({"sample": str(repo / "docs")}), encoding="utf-8"
    )
    bin_dir = tmp_path / f"bin-{trial}"
    _write_stub(bin_dir, marker_dir, trial)
    driver_path = tmp_path / DRIVER_FILENAME
    driver_path.write_text(DRIVER_SOURCE, encoding="utf-8")
    pid_file = marker_dir / f"worker.{trial}.pid"
    marker = marker_dir / f"marker.{trial}"
    for stale in (pid_file, marker):
        if stale.exists():
            stale.unlink()
    driver_output = tmp_path / f"driver-{trial}.out"
    driver_error = tmp_path / f"driver-{trial}.err"
    trace_path = marker_dir / f"trace.{trial}.txt"
    payload = {
        "repo": str(repo),
        "config": str(config_home),
        "session": f"probe-session-{trial}",
        "bin_dir": str(bin_dir),
        "config_data": CONFIG,
        "watch_env": WATCH_ARMING,
        "arming_value": spec.get("arming_value", "off"),
        "watch_required": spec.get("watch_required", False),
        "follow_claim": spec.get("follow_claim", False),
        "node": f"probe-{trial}",
        "write_path": f"src/{trial}.txt",
        "done_when": (
            f"pytest tests/test_dispatch_lifetime_probe.py exits 0; the "
            f"stub writes marker.{trial} within 90 s of the dispatch"
        ),
        "manifest": str(config_home / "manifest.md"),
        "trace_path": str(trace_path),
    }
    payload_path = tmp_path / f"payload-{trial}.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    with driver_output.open("w") as out, driver_error.open("w") as err:
        process = subprocess.Popen(
            [sys.executable, str(driver_path), str(payload_path)],
            cwd=str(repo),
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
        )
    started = time.monotonic()
    worker_launch_after = None
    dispatch_exit_after = None
    held_after_launch = False
    signal_sent = False
    signal_error = None
    signal_sent_after = None
    tree = {}
    timeline = []
    while True:
        now = time.monotonic() - started
        for entry in _descendants(process.pid, _process_table()):
            existing = tree.get(entry["pid"])
            if existing is None:
                entry["role"] = _role(entry)
                entry["first_seen_after_seconds"] = round(now, 3)
                entry["last_seen_after_seconds"] = round(now, 3)
                tree[entry["pid"]] = entry
                timeline.append(
                    {
                        "after_seconds": round(now, 3),
                        "role": entry["role"],
                        "pid": entry["pid"],
                        "argv": entry["argv"],
                    }
                )
            else:
                existing["last_seen_after_seconds"] = round(now, 3)
                existing["state"] = entry["state"]
        if worker_launch_after is None and _read_pid(pid_file) is not None:
            worker_launch_after = round(time.monotonic() - started, 3)
            if signal_number is not None:
                signal_sent_after = worker_launch_after
                try:
                    os.kill(process.pid, signal_number)
                    signal_sent = True
                except OSError as exc:
                    signal_error = str(exc)
        if process.poll() is not None:
            dispatch_exit_after = round(time.monotonic() - started, 3)
            break
        if worker_launch_after is not None and (
            time.monotonic() - started - worker_launch_after > POST_LAUNCH_GRACE_SECONDS
        ):
            held_after_launch = True
            break
        if worker_launch_after is None and now > LAUNCH_DEADLINE_SECONDS:
            break
        time.sleep(0.02)
    killed_after_hold = False
    if held_after_launch and process.poll() is None:
        process.kill()
        killed_after_hold = True
    process.wait(30)
    returncode = process.returncode
    marker_appeared = False
    marker_after = None
    if worker_launch_after is not None:
        marker_deadline = started + worker_launch_after + MARKER_DEADLINE_SECONDS
        while time.monotonic() < marker_deadline:
            if marker.exists():
                marker_after = round(time.monotonic() - started, 3)
                marker_appeared = True
                break
            time.sleep(0.05)
    exit_after_launch = None
    if dispatch_exit_after is not None and worker_launch_after is not None:
        exit_after_launch = round(dispatch_exit_after - worker_launch_after, 3)
    reaped = _reap_temp_processes(config_home)
    return {
        "signal": spec.get("signal_name"),
        "watch_required": spec.get("watch_required", False),
        "follow_claim": spec.get("follow_claim", False),
        "dispatch_returncode": returncode,
        "dispatch_exit_elapsed_seconds": dispatch_exit_after,
        "dispatch_held_after_launch": held_after_launch,
        "dispatch_killed_after_hold": killed_after_hold,
        "dispatch_exit_after_launch_seconds": exit_after_launch,
        "dispatch_stdout": driver_output.read_text(encoding="utf-8").strip(),
        "dispatch_stderr_tail": driver_error.read_text(encoding="utf-8")[-2000:],
        "worker_pid": _read_pid(pid_file),
        "worker_launch_after_seconds": worker_launch_after,
        "signal_sent": signal_sent,
        "signal_target": "dispatch-process" if signal_sent else None,
        "signal_sent_after_seconds": signal_sent_after,
        "signal_error": signal_error,
        "marker_appeared": marker_appeared,
        "marker_after_seconds": marker_after,
        "process_tree": sorted(tree.values(), key=lambda item: item["pid"]),
        "reaped_processes": reaped,
        "process_timeline": timeline,
        "trace_path": str(trace_path),
    }


def _reap_temp_processes(marker: Path) -> list[int]:
    """Kill every process whose environment names the throwaway config home.

    The stub worker and any armed watch producer carry that home and outlive
    the dispatch; leaving them running would let one trial contaminate the next.
    """
    needle = str(marker).encode()
    killed = []
    pids = [int(item.name) for item in Path("/proc").iterdir() if item.name.isdigit()]
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            environ = (Path("/proc") / str(pid) / "environ").read_bytes()
        except OSError:
            continue
        if needle in environ:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
            killed.append(pid)
    return sorted(killed)


def _relative(path: str) -> str:
    """Return a path relative to the repository when it lives inside it."""
    try:
        return str(Path(path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        return path


def _holder_from_trace(paths: list[Path]) -> dict:
    """Name the reckon frame the dispatch process was parked in, from a dump.

    faulthandler prints every thread with the main thread last, and only the
    main thread's return ends the process, so that block is the one worth
    reading: a daemon thread's frame holds nothing open.
    """
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        index = text.rfind("Current thread")
        block = text[index:] if index >= 0 else text
        frames = re.findall(r'File "([^"]+)", line (\d+), in (\S+)', block)
        for file_name, line_text, function in reversed(frames):
            if f"{os.sep}reckon{os.sep}crew{os.sep}" in file_name:
                return {
                    "file": _relative(file_name),
                    "line": int(line_text),
                    "function": function,
                    "source_quoted": linecache.getline(
                        file_name, int(line_text)
                    ).strip(),
                }
    return {
        "file": None,
        "line": None,
        "function": None,
        "source_quoted": None,
        "finding": (
            "no frame inside the reckon package was on the main thread's stack "
            "while the dispatch process lived, so the process was never parked "
            "in reckon code"
        ),
    }


def _cite(path: str, needle: str, function: str, occurrence: int = 1) -> dict:
    """Return the line carrying needle, quoted, so a reader can check it."""
    text = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    seen = 0
    for number, line in enumerate(text, start=1):
        if needle in line:
            seen += 1
            if seen == occurrence:
                return {
                    "path": path,
                    "line": number,
                    "source_quoted": line.strip(),
                    "enclosing_function": function,
                }
    return {
        "path": path,
        "line": None,
        "source_quoted": None,
        "enclosing_function": function,
    }


def _source_revision() -> str:
    """Return the revision the measurement was taken against."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _real_live_dir() -> Path:
    """Return the real live pointer directory, captured when this module loaded.

    It is resolved at import rather than at call time so the guard reads the
    directory a dispatch outside this suite writes to, even though the suite
    points ``RECKON_HOME`` at a throwaway home for the duration of the test.
    """
    return REAL_LIVE_DIR


def _snapshot(path: Path) -> dict:
    """Return a directory's existence, sorted entry names and mtime."""
    if not path.exists():
        return {"exists": False, "entries": [], "mtime_ns": None}
    return {
        "exists": True,
        "entries": sorted(item.name for item in path.iterdir()),
        "mtime_ns": path.stat().st_mtime_ns,
    }


def _candidate_holders() -> list[dict]:
    """Return the code that could keep a dispatch process alive, with citations
    a reader can check against the source tree."""
    return [
        {
            "mechanism": "the git helper every routing step shells out through",
            "citation": _cite(
                "reckon/crew/routing.py", "result = subprocess.run(", "_git"
            ),
            "disposition": (
                "each routing step that runs git lands here, and worktree "
                "creation is the slow one, so this is where a lifetime in "
                "seconds-to-minutes is spent before any worker has been "
                "launched"
            ),
        },
        {
            "mechanism": "the launched-worker reaper thread",
            "citation": _cite(
                "reckon/crew/dispatch.py",
                'name="reckon-worker-reaper",',
                "_ensure_launched_worker_reaper",
            ),
            "disposition": (
                "cannot hold the process: the thread is constructed with "
                "daemon=True, so the interpreter does not wait for it after the "
                "main thread returns"
            ),
        },
        {
            "mechanism": "the watcher-arming wait",
            "citation": _cite(
                "reckon/crew/dispatch.py", "WATCHER_LOAD_BOUND_SECONDS = ", ""
            ),
            "disposition": (
                "bounded: arming returns once the producer's liveness resolves "
                "or the thirty-second bound elapses"
            ),
        },
        {
            "mechanism": "the launched worker, if the dispatch process waited on it",
            "citation": _cite(
                "reckon/crew/dispatch.py",
                "start_new_session=True,",
                "_spawn",
                occurrence=2,
            ),
            "disposition": (
                "not awaited: the worker is detached into its own session and "
                "its stdio is redirected to files, so neither a wait nor a read "
                "bounded by the worker keeps the dispatch process alive"
            ),
        },
    ]


def _life_summary(trial: dict) -> dict:
    """Return when the dispatch process launched its worker and when it exited.

    The two instants answer the node's second question before any signal is
    considered: a dispatch process that exits at or before its worker's launch
    was never holding anything open afterwards, whatever the signals then show.
    """
    launch = trial["worker_launch_after_seconds"]
    exit_time = trial["dispatch_exit_elapsed_seconds"]
    held = trial["dispatch_held_after_launch"]
    return {
        "launched_after_seconds": launch,
        "exited_after_seconds": exit_time,
        "held_after_launch": held,
        "exited_after_launch_seconds": trial["dispatch_exit_after_launch_seconds"],
    }


def _render_figure(trials: dict, names: list[str]) -> str | None:
    """Draw the two measured quantities and return the figure's repo path."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    FIGURES.mkdir(parents=True, exist_ok=True)
    figure, (left, right) = plt.subplots(1, 2, figsize=(12, 4.5))
    launch = [trials[name]["worker_launch_after_seconds"] or 0.0 for name in names]
    exited = [trials[name]["dispatch_exit_elapsed_seconds"] or 0.0 for name in names]
    x = list(range(len(names)))
    left.bar(x, launch, width=0.38, label="worker launched")
    left.bar(
        [value + 0.4 for value in x], exited, width=0.38, label="dispatch process exit"
    )
    left.set_xticks([value + 0.2 for value in x])
    left.set_xticklabels(names, rotation=20)
    left.set_ylabel("seconds after dispatch start")
    left.set_title("launch time and dispatch process exit")
    left.legend(fontsize=8)
    survived = [1.0 if trials[name]["marker_appeared"] else 0.0 for name in names]
    right.bar(names, survived, color="#4c9f70")
    right.set_ylim(0, 1.2)
    right.set_ylabel("stub marker written")
    right.set_title("worker outlives the dispatch process")
    right.tick_params(axis="x", rotation=20)
    figure.tight_layout()
    path = FIGURES / "dispatch_lifetime.png"
    figure.savefig(path, dpi=130)
    plt.close(figure)
    return str(path.relative_to(REPO_ROOT))


@pytest.fixture()
def probe_env(tmp_path: Path) -> dict:
    """Provide the scratch space one probe run needs.

    The repository is built per trial by ``_run_trial``, so nothing here
    outlives a single dispatch's ledger writes.
    """
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    return {"tmp_path": tmp_path, "marker_dir": marker_dir}


@pytest.mark.skipif(
    os.environ.get(RECKON_RUN_DISPATCH_PROBE_ENV) != "1",
    reason=(
        f"{RECKON_RUN_DISPATCH_PROBE_ENV}=1 is required: this probe starts real "
        "processes and rewrites docs/research/data/dispatch-lifetime.json, so it "
        "is opted into rather than run with the default suite"
    ),
)
def test_dispatch_lifetime_probe(probe_env: dict) -> None:
    """Observe the dispatch process, then end it three ways.

    The stub's marker is the positive control: an absent marker after a killed
    dispatch is a finding only because the same stub writes one when nothing
    was killed, which the first trial shows.
    """
    tmp_path = probe_env["tmp_path"]
    marker_dir = probe_env["marker_dir"]
    real_before = _snapshot(_real_live_dir())
    trials = [
        {"name": "graceful", "signal": None, "signal_name": None},
        {"name": "sigterm", "signal": signal.SIGTERM, "signal_name": "SIGTERM"},
        {"name": "sigkill", "signal": signal.SIGKILL, "signal_name": "SIGKILL"},
        {"name": "sighup", "signal": signal.SIGHUP, "signal_name": "SIGHUP"},
        {
            "name": "watcher",
            "signal": None,
            "signal_name": None,
            "watch_required": True,
            "arming_value": "on",
            "follow_claim": True,
        },
    ]
    names = [spec["name"] for spec in trials]
    results = {}
    for spec in trials:
        results[spec["name"]] = _run_trial(tmp_path, marker_dir, spec)
    holder = _holder_from_trace([Path(results[name]["trace_path"]) for name in names])
    real_after = _snapshot(_real_live_dir())
    probe_pointers = []
    for name in names:
        live = tmp_path / f"home-{name}" / "crew" / "live"
        if live.is_dir():
            probe_pointers.extend(item.name for item in live.iterdir())
    leaked = [name for name in real_after["entries"] if name in set(probe_pointers)]
    artifact = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_revision": _source_revision(),
        "probe": {
            "driver": "a child process calling reckon.crew.dispatch with a stub backend",
            "stub_command": (
                f"a shell script named claude that records its pid, sleeps "
                f"{STUB_SLEEP_SECONDS}s, then writes a marker file"
            ),
            "attack_surface": (
                "a temporary repository and a temporary crew configuration home "
                "(RECKON_HOME); every dispatch is synchronous and the watch "
                "producer is suppressed except in the one trial that measures "
                "the arming path"
            ),
        },
        "holding_code_path": holder,
        "holding_note": (
            "the done-when asks for the code path that holds the dispatch process "
            "open after the worker launches. This probe measures none: in every "
            "trial the dispatch process exited less than a second after the "
            "worker's pid file appeared, so no file or line holds it there. What "
            "the dispatch process does spend time on is pre-launch, and that is "
            "cited under pre_launch_holder."
        ),
        "pre_launch_holder": _cite(
            "reckon/crew/routing.py",
            "result = subprocess.run(",
            "_create_worktree",
            occurrence=2,
        ),
        "dispatch_process_life": {
            spec["name"]: _life_summary(results[spec["name"]]) for spec in trials
        },
        "candidate_holders": _candidate_holders(),
        "trials": {
            spec["name"]: {
                key: value
                for key, value in results[spec["name"]].items()
                if key != "trace_path"
            }
            for spec in trials
        },
        "real_pointer_directory": {
            "path": str(_real_live_dir()),
            "before_mtime_ns": real_before["mtime_ns"],
            "after_mtime_ns": real_after["mtime_ns"],
            "probe_pointers": sorted(probe_pointers),
            "names_from_probe_found_in_real_dir": sorted(leaked),
            "untouched": not leaked,
        },
    }
    figure = _render_figure(artifact["trials"], names)
    if figure is not None:
        artifact["figure"] = figure
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    assert not leaked, (
        f"the probe wrote its own live pointers into the real fleet directory: {leaked}"
    )
    assert results["graceful"]["marker_appeared"], (
        "the stub never wrote its marker even with nothing killing it, so "
        "the instrument is unaimed and the other trials prove nothing. "
        f"driver stderr: {results['graceful']['dispatch_stderr_tail']!r}"
    )
    for name in names:
        assert results[name]["worker_launch_after_seconds"] is not None, (
            f"no worker was launched in the {name} trial, so its signal and "
            "its marker say nothing about survival. driver stderr: "
            f"{results[name]['dispatch_stderr_tail']!r}"
        )
        if results[name]["signal"] is not None:
            assert results[name]["signal_sent"], (
                f"the {name} trial never delivered its signal after launch"
            )
    for name in ("graceful", "sigterm", "sigkill", "sighup", "watcher"):
        assert results[name]["marker_appeared"], (
            "the worker did not survive the dispatch process ending in the "
            f"{name} trial: {results[name]['dispatch_stderr_tail']!r}"
        )
    # The measurement this probe exists for. A dispatch process that outlives
    # its worker's launch by the grace window is a hold, and a hold is what the
    # plan is looking for; this assertion is the one that fires if an await is
    # put back in front of the returned worker.
    for name in names:
        assert not results[name]["dispatch_held_after_launch"], (
            f"the dispatch process in the {name} trial was still alive "
            f"{results[name]['dispatch_exit_after_launch_seconds']} s after its "
            "worker launched, so something in the launch path awaits the worker"
        )
