"""Gate: a CLI dispatch returns once its per-run supervisor is running.

The cases below run against a throwaway repository and a throwaway
configuration home, with the same stub backend the dispatch probe uses. Four of
them measure the spawned lane; the last measures the delegated one, which
spawns no process and must still take its own boundary baseline. Three
instruments make the measurements mean something:

* a ``git`` shim first on the child processes' ``PATH`` that records the pid
  that ran each ``status`` call and, in the case that measures the return bound,
  signals and holds every charged call the boundary scan makes under the case's
  base until that case releases it. A fenced run's supervisor snapshots exactly
  two trees — its own worktree and the main checkout — both under that root, so
  the two-tree scan cannot finish inside dispatch's return bound and the case
  observes dispatch's return with the scan provably still in progress. The hold
  skips calls made by the dispatch process itself, so dispatch's own pre-return
  calls are charged but never block its return;
* a ``sitecustomize`` module on the driver's ``PYTHONPATH`` that parks a
  dispatch process immediately after it has started its supervisor, so a
  signal can be delivered while dispatch is still inside ``crew.dispatch`` —
  the tail after the spawn is microseconds long, and a test that merely polled
  for the supervisor would always arrive after the process had left;
* the run directory itself, which is where the supervisor writes everything it
  produces and therefore the only place the boundary snapshot may be read from.

The repository is shared by the cases and its worktrees are built once, because
registering thirty worktrees is the expensive part and every case needs the
same slowed scan. Each case takes its own configuration home, so no case sees
another's pointers or run directories.

Everything the cases start is killed afterwards, and the real (non-temporary)
live-pointer directory is asserted untouched, so a passing run leaves the host
as it found it.
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import reckon
from reckon._store import _config_home
from reckon.crew import promotion, promotion_scope
from reckon.crew.node import CrewError
from reckon.shim_lookup import real_executable

# The package the cases exercise, resolved from the interpreter running them
# rather than from the repository path, because the gate also runs against a
# copy of the package carrying a deliberate defect and must then measure that
# copy.
PACKAGE_ROOT = Path(reckon.__file__).resolve().parents[1]

# The module under test, imported as the gate's own package so the survival
# check is exercised against the tree this case imported rather than any other.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

# Captured at import, before the suite's autouse home fixture redirects
# ``RECKON_HOME``. The guard at the end of the first case must name the
# directory a dispatch outside this suite writes to; resolving it at call time
# would name the fixture's throwaway home instead, and the guard would hold
# because the cases write nowhere near the real fleet rather than because they
# left it alone.
REAL_LIVE_DIR = _config_home() / "crew" / "live"


@pytest.fixture()
def writable_marker_dir() -> Path:
    with tempfile.TemporaryDirectory(
        prefix="reckon-worker-marker-", dir=os.environ.get("TMPDIR")
    ) as directory:
        yield Path(directory)

PYTHON = Path(sys.executable)

PROJECT = "sample"
STUB_COMMAND = "claude"
SUPERVISOR_ENTRY = "__supervise__"

# The scan crosses one worktree per registered tree, and the repository carries
# well over thirty so that the scan a case holds open is long enough that a
# dispatch which waited for it would blow its return bound.
WORKTREE_COUNT = 30

# Dispatch's own return bound, on dispatch's clock, is derived from a reference
# measured on this host at this moment rather than fixed. One reference unit is
# a fresh interpreter importing the package under test — the class of process
# dispatch's pre-return path starts (the worktree script, the launcher
# intermediate, the supervisor) — and the reference is the slowest of a few
# samples taken beside the dispatch, so the bound follows the host's load. A
# fixed bound cannot carry this: at load near 50 this case's dispatch measured
# about 10.9 s against a fixed 8.0 s and failed as readily at the base revision
# as at the fix, so the constant was measuring the host rather than dispatch.
# REFERENCE_UNITS is generous beside the pre-return work dispatch measured on
# this host and far below the roughly thirty-second hold a dispatch that joined
# its supervisor must outlast, so that failure is still caught decisively.
REFERENCE_IMPORT = "import reckon.crew.dispatch"
REFERENCE_SAMPLES = 3
REFERENCE_UNITS = 8.0
MARKER_BOUND = 60.0
EXIT_RECORD_BOUND = 10.0
COMPLETION_RECORD_BOUND = 120.0
SPAWN_BOUND = 60.0
DISCARD_BOUND = 60.0

# The driver's own wall bound: dispatch's return bound, plus the time the driver
# spends importing the package under test before it calls dispatch (reported as
# ``startup_seconds``), plus fork, exec and output-write slack. Interpreter
# startup is the driver's cost, not dispatch's, so it is measured and added
# rather than charged to the return bound; dispatch's own clock is bounded
# exactly and separately below.
DRIVER_EXIT_SLACK = 1.5

# The shim holds a charged scan call until the case releases it, polling every
# 0.02 s for this many polls. The bound is about thirty seconds, so a case that
# never releases cannot wedge the scan forever.
SHIM_GATE_WAITS = 1500

# Every ``git status`` dispatch makes before it returns is charged and recorded
# by the shim, so the count of them bounds the work dispatch does in its own
# pre-return path. The done-when declares four.
PRE_RETURN_STATUS_BOUND = 4

# How long the driver stays parked after starting its supervisor, waiting for
# the case to deliver a kill. Long enough that a slow host still lands inside
# the hold, short enough that a case which misses it still finishes.
HOLD_SECONDS = 120.0

# The report the gate writes beside its temporary homes, so the manifest can
# name the pre-return status calls the shim charged and the outcome of each
# case rather than paraphrasing them.
REPORT_ENV = "RECKON_GATE_REPORT"


CONFIG: dict[str, Any] = {
    "default_backend": "stub",
    "backends": {
        "stub": {
            "launch": "cli",
            "command": STUB_COMMAND,
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


# The delegated lane: the class of backend that spawns no process because the
# calling harness runs the run itself. It declares no command, exactly as the
# shipped default backend does, so nothing about this case can be satisfied by
# a spawned process.
IN_HARNESS_CONFIG: dict[str, Any] = {
    "default_backend": "delegated",
    "backends": {
        "delegated": {
            "launch": "in-harness",
            "sandbox": "worktree-full",
            "session_reuse": False,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


DRIVER_SOURCE = """\
import json
import os
import sys
import time
from pathlib import Path

# Taken before the package under test is imported, so the case can subtract the
# driver's own startup from its wall time and bound dispatch's return on
# dispatch's clock rather than on the interpreter's.
_boot = time.monotonic()

payload = json.loads(Path(sys.argv[1]).read_text())
os.environ["RECKON_HOME"] = payload["config"]
os.environ["PATH"] = payload["bin_dir"] + os.pathsep + os.environ.get("PATH", "")
# This dispatch process's own pid, so the shim can tell the supervisor's
# boundary scan (a child of this process) apart from dispatch's own pre-return
# calls and hold only the scan.
os.environ["RECKON_DISPATCH_PID"] = str(os.getpid())

import reckon
from reckon import crew
from reckon.crew.node import TaskNode

node = TaskNode(
    id=payload["node"],
    goal="record one stub-backed dispatch",
    plan="fixture",
    section="supervisor",
    role="implement",
    spec_level="guided",
    done_when=payload["done_when"],
    write_paths=[payload["write_path"]],
    time_budget="25m",
    manifest_path=payload["manifest"],
)
started = time.monotonic()
record = crew.dispatch(
    node=node,
    project=payload["project"],
    repo=payload["repo"],
    config=payload["config_data"],
    session=payload["session"],
    watch_required=False,
)
elapsed = time.monotonic() - started
Path(payload["out_path"]).write_text(
    json.dumps(
        {
            "run_id": record.get("run_id"),
            "pid": record.get("pid"),
            "startup_seconds": started - _boot,
            "dispatch_seconds": elapsed,
            "reckon_file": reckon.__file__,
        }
    ),
    encoding="utf-8",
)
"""


# ``sitecustomize`` is imported by every interpreter that has its directory on
# the path, so it must do nothing at all unless the case that needs the hold
# set the flag. Registered under that name deliberately: it is the one hook an
# interpreter runs before the program under measurement does anything.
SITECUSTOMIZE_SOURCE = '''\
"""Park a dispatch process after it starts its supervisor, for one measurement."""

import os
import time
from pathlib import Path

_FLAG = os.environ.get("RECKON_GATE_HOLD_FLAG")
_SECONDS = float(os.environ.get("RECKON_GATE_HOLD_SECONDS") or "60")

if _FLAG:
    # The hold is taken at the seam that starts the supervisor, not at the
    # process-spawn call beneath it, so it survives a change to how the
    # supervisor is spawned. Imported lazily so an interpreter that never
    # reaches dispatch — a child of the launcher, say — pays nothing.
    try:
        import importlib

        _dispatch = importlib.import_module("reckon.crew.dispatch_launch")
        _spawn = _dispatch._spawn_detached_supervisor

        def _holding(argv, stderr_path):
            pid = _spawn(argv, stderr_path)
            Path(_FLAG).write_text(str(pid) + "\\n", encoding="utf-8")
            deadline = time.monotonic() + _SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.02)
            return pid

        _dispatch._spawn_detached_supervisor = _holding
    except Exception:
        pass
'''


SHIM_SOURCE = """#!/bin/sh
# Charge every ``git status`` and record who ran it. The first field is the
# parent pid, which separates the dispatch process's own calls from the
# supervisor's scan: the two run concurrently after the spawn.
charge=0
prev=""
for a in "$@"; do
  case "$prev" in
    -C|--git-dir|--work-tree|-c) prev="$a"; continue ;;
  esac
  if [ "$a" = "status" ]; then charge=1; fi
  prev="$a"
done
if [ "$charge" = "1" ]; then
  here=$(pwd)
  printf '%s\\t%s\\t%s\\t%s\\n' "$PPID" "$(date +%s.%N)" "$here" "$*" \\
    >> "$RECKON_SHIM_LOG"
  if [ -n "$RECKON_SHIM_SUPERVISOR_CPU" ]; then
    case "$(ps -o args= -p "$PPID")" in
      *"__supervise__"*)
        taskset -pc "$RECKON_SHIM_SUPERVISOR_CPU" "$PPID" \\
          >> "$RECKON_SHIM_LOG.affinity" 2>&1
        renice -n 15 -p "$PPID" >> "$RECKON_SHIM_LOG.affinity" 2>&1
        ;;
    esac
  fi
  # A call run from under the scan root by a process that is not the dispatch
  # process is the boundary scan's: a fenced supervisor scans its own worktree
  # and the main checkout, while dispatch's own pre-return calls are made by the
  # dispatch process itself. Signal that the scan is in progress and hold it
  # until the case releases it, so the case observes dispatch's return while the
  # scan is still running rather than waiting for it to finish. Dispatch's own
  # calls are never held, or dispatch would block before it could return.
  if [ -n "$RECKON_SHIM_SCAN_ROOT" ]; then
    hold=1
    case "$here" in
      "$RECKON_SHIM_SCAN_ROOT"/*) ;;
      *) hold=0 ;;
    esac
    if [ "$hold" = "1" ] && [ -n "$RECKON_DISPATCH_PID" ] \
        && [ "$PPID" = "$RECKON_DISPATCH_PID" ]; then
      hold=0
    fi
    if [ "$hold" = "1" ]; then
      : > "$RECKON_SHIM_SCAN_SIGNAL"
      waited=0
      while [ ! -e "$RECKON_SHIM_SCAN_RELEASE" ]; do
        [ "$waited" -ge "$RECKON_SHIM_SCAN_WAITS" ] && break
        sleep 0.02
        waited=$((waited + 1))
      done
    fi
  fi
fi
exec "$RECKON_SHIM_GIT" "$@"
"""


# --------------------------------------------------------------------------
# Shared host: one repository, thirty-odd worktrees, one slow git.
# --------------------------------------------------------------------------


def _real_git() -> str:
    """Return the real git's absolute path, never a reckon shim.

    Inside a crew worker the first ``git`` on ``PATH`` is the main checkout's
    shim, which is not the binary this fixture means to time.
    """
    found = real_executable("git", os.environ.get("PATH", ""))
    assert found, "git is not on PATH, so the worktrees cannot be built"
    return found


def _run_git(repo: Path, *arguments: str) -> None:
    subprocess.run([_real_git(), *arguments], cwd=repo, check=True, capture_output=True)


def _registered_worktree_count(repo: Path) -> int:
    listed = subprocess.run(
        [_real_git(), "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sum(1 for line in listed.splitlines() if line.startswith("worktree "))


def _build_repository(repo: Path, worktree_root: Path) -> None:
    """Create a minimal dispatchable repository with a slowed boundary scan."""
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "fixture.html").write_text(
        '<meta name="docs-project" content="sample">'
        '<meta name="reckon-type" content="plan">'
        '<meta name="plan-slug" content="fixture">'
        '<h2 id="supervisor">A dispatch returns before its supervisor finishes</h2>',
        encoding="utf-8",
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    # Dispatch reads the fleet script out of the repository it dispatches, so
    # the throwaway repository carries the same copy the real one does.
    fleet = PACKAGE_ROOT / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py"
    if fleet.is_file():
        destination = repo / "skills" / "reckon-build" / "scripts"
        destination.mkdir(parents=True)
        shutil.copy(fleet, destination / "worktree_fleet.py")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("add", "seed.txt", "docs/plans/fixture.html"),
        (
            "-c",
            "user.email=worker@example.invalid",
            "-c",
            "user.name=Worker",
            "commit",
            "-q",
            "-m",
            "chore: seed",
        ),
    ):
        _run_git(repo, *arguments)
    # The worktrees are registered without a checkout: the scan reads each
    # registered tree's status, and nothing in the scan depends on the files
    # being present, so populating thirty of them would only slow the host.
    worktree_root.mkdir(parents=True, exist_ok=True)
    for index in range(WORKTREE_COUNT):
        _run_git(
            repo,
            "worktree",
            "add",
            "--detach",
            "--no-checkout",
            str(worktree_root / f"registered-{index:02d}"),
            "HEAD",
        )


@pytest.fixture(scope="module")
def host(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """One repository with a slowed scan, shared by every case."""
    base = tmp_path_factory.mktemp("dispatch-returns")
    repo = base / "repo"
    repo.mkdir()
    outcomes: list[dict[str, Any]] = []
    _build_repository(repo, base / "worktrees")
    registered = _registered_worktree_count(repo)
    assert registered >= WORKTREE_COUNT, (
        f"the repository registered {registered} worktrees, and the scan must "
        f"outlast dispatch's return bound, which needs at least {WORKTREE_COUNT}"
    )
    bin_dir = base / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(SHIM_SOURCE, encoding="utf-8")
    shim.chmod(0o755)
    (bin_dir / "sitecustomize.py").write_text(SITECUSTOMIZE_SOURCE, encoding="utf-8")
    driver = base / "dispatch_driver.py"
    driver.write_text(DRIVER_SOURCE, encoding="utf-8")
    yield {
        "base": base,
        "repo": repo,
        "bin_dir": bin_dir,
        "driver": driver,
        "real_git": _real_git(),
        "outcomes": outcomes,
    }
    leaked = sorted(
        name
        for outcome in outcomes
        for name in outcome.get("own_pointer_names", [])
        if (REAL_LIVE_DIR / name).exists()
    )
    _write_report(outcomes)
    assert not leaked, (
        "these runs wrote their own pointers into the real fleet's live "
        f"directory under {REAL_LIVE_DIR}: {leaked}. Every case ran under a "
        "temporary RECKON_HOME, so none of them may touch the real one."
    )


def _write_report(outcomes: list[dict[str, Any]]) -> None:
    """Record what the cases measured where the manifest can read it."""
    report = Path(
        os.environ.get(REPORT_ENV)
        or Path(tempfile.gettempdir()) / "reckon-dispatch-gate.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "package_root": str(PACKAGE_ROOT),
                "python": str(PYTHON),
                "real_live_dir": str(REAL_LIVE_DIR),
                "cases": outcomes,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stderr.write(f"\ndispatch-return gate report: {report}\n")


# --------------------------------------------------------------------------
# Per-case host: a configuration home, a stub, and a running dispatch.
# --------------------------------------------------------------------------


def _case_home(host: dict[str, Any], tag: str) -> Path:
    home = host["base"] / f"home-{tag}"
    home.mkdir(parents=True, exist_ok=True)
    (home / "mounts.json").write_text(
        json.dumps({PROJECT: str(host["repo"] / "docs")}), encoding="utf-8"
    )
    return home


def _write_stub(bin_dir: Path, marker_dir: Path, tag: str, sleep_seconds: int) -> None:
    """Write the stub backend: record its pid, sleep, then write its marker."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / STUB_COMMAND
    script.write_text(
        "#!/bin/sh\n"
        f'echo $$ > "{marker_dir}/worker.{tag}.pid"\n'
        f"sleep {sleep_seconds}\n"
        f': > "{marker_dir}/marker.{tag}"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


class _Run:
    """A dispatch process and everything it started."""

    def __init__(
        self,
        *,
        tag: str,
        home: Path,
        marker_dir: Path,
        process: subprocess.Popen,
        started: float,
    ) -> None:
        self.tag = tag
        self.home = home
        self.marker_dir = marker_dir
        self.process = process
        self.started = started
        self.pointer: dict[str, Any] | None = None
        self.stdout_path = home / f"driver-{tag}.out"
        self.stderr_path = home / f"driver-{tag}.err"
        # The shim writes ``scan_signal`` when the boundary scan's first charged
        # call runs under the worktree root, and holds that call until
        # ``scan_release`` exists. A case that starts no gated scan never sees
        # either file.
        self.scan_signal = home / "scan-gate" / "scanning"
        self.scan_release = home / "scan-gate" / "release"

    @property
    def marker(self) -> Path:
        return self.marker_dir / f"marker.{self.tag}"

    def marker_present(self) -> bool:
        return self.marker.exists()

    def scan_started(self) -> bool:
        """True once the shim has signalled a charged scan call is in progress."""
        return self.scan_signal.exists()

    def release_scan(self) -> None:
        """Let the held boundary scan finish. Idempotent."""
        self.scan_release.parent.mkdir(parents=True, exist_ok=True)
        self.scan_release.touch()

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def output(self) -> dict[str, Any] | None:
        return _load_json(self.home / f"driver-{self.tag}.json")

    def run_id(self) -> str | None:
        if self.pointer is not None and self.pointer.get("run_id"):
            return str(self.pointer["run_id"])
        output = self.output() or {}
        return str(output["run_id"]) if output.get("run_id") else None

    def run_directory(self) -> Path:
        run_id = self.run_id()
        assert run_id, "no run id was ever observed for this dispatch"
        return self.home / "crew" / "runs" / run_id

    def pointer_path(self) -> Path:
        run_id = self.run_id()
        return self.home / "crew" / "live" / f"{run_id}.json"

    def own_live_pointers(self) -> list[Path]:
        """Every live pointer this case wrote, in its own private home.

        This case's home holds only this case's runs, so these names are exactly
        the ones that must never appear in the real fleet's live directory.
        """
        live = self.home / "crew" / "live"
        return sorted(live.glob("*.json")) if live.is_dir() else []

    def own_pointer_names(self) -> set[str]:
        """Every live-pointer name this case may have written, present or not.

        The run id is added unconditionally so a case that deletes its own
        pointer still names it — the point of the guard is that the real
        fleet's directory never gains these names.
        """
        names = {path.name for path in self.own_live_pointers()}
        run_id = self.run_id()
        if run_id:
            names.add(f"{run_id}.json")
        return names

    def read_pointer(self) -> dict[str, Any] | None:
        """Read the pointer this case's dispatch wrote, from its own home."""
        live = self.home / "crew" / "live"
        if not live.is_dir():
            return None
        for path in sorted(live.glob("*.json")):
            record = _load_json(path)
            if isinstance(record, dict) and record.get("run_id"):
                return record
        return None

    def shim_lines(self) -> list[dict[str, str]]:
        return _read_shim_lines(self.home / f"shim-{self.tag}.log")

    def survivors(self) -> list[int]:
        return _reap_processes(self.home)

    def stderr(self) -> str:
        return _tail(self.stderr_path)


def _read_shim_lines(log: Path) -> list[dict[str, str]]:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 4:
            continue
        lines.append(
            {
                "pid": fields[0],
                "epoch": fields[1],
                "cwd": fields[2],
                "argv": fields[3],
            }
        )
    return lines


def _start_dispatch(
    host: dict[str, Any],
    *,
    tag: str,
    marker_dir: Path,
    stub_sleep: int,
    hold: bool,
    scan_gate: bool = False,
    config_data: dict[str, Any] | None = None,
) -> _Run:
    home = _case_home(host, tag)
    stub_dir = host["base"] / f"bin-{tag}"
    _write_stub(stub_dir, marker_dir, tag, stub_sleep)
    # The stub directory goes first so ``claude`` resolves to the stub and
    # ``git`` to the shim; the package root follows so every child imports the
    # same tree the case itself imported.
    environment = {
        **os.environ,
        "RECKON_HOME": str(home),
        "PATH": f"{stub_dir}{os.pathsep}{host['bin_dir']}{os.pathsep}"
        + os.environ.get("PATH", ""),
        "PYTHONPATH": f"{host['bin_dir']}{os.pathsep}{PACKAGE_ROOT}",
        "RECKON_SHIM_LOG": str(home / f"shim-{tag}.log"),
        "RECKON_SHIM_GIT": host["real_git"],
        "RECKON_WATCH_ARMING": "off",
    }
    if scan_gate:
        # The shim holds each charged call the boundary scan makes under the
        # scan root until this case releases it, so the case can observe
        # dispatch's return while the scan is still in progress. A fenced run's
        # supervisor snapshots only its own worktree and the main checkout, both
        # under the case's base, so the scan root is the base; the shim holds
        # only calls made by a process other than the dispatch process, so
        # dispatch's own pre-return calls are charged but never held and dispatch
        # is free to return while the scan waits.
        (home / "scan-gate").mkdir(parents=True, exist_ok=True)
        environment["RECKON_SHIM_SCAN_ROOT"] = str(host["base"])
        environment["RECKON_SHIM_SCAN_SIGNAL"] = str(home / "scan-gate" / "scanning")
        environment["RECKON_SHIM_SCAN_RELEASE"] = str(home / "scan-gate" / "release")
        environment["RECKON_SHIM_SCAN_WAITS"] = str(SHIM_GATE_WAITS)
    payload = {
        "repo": str(host["repo"]),
        "config": str(home),
        "project": PROJECT,
        "session": f"gate-session-{tag}",
        "bin_dir": str(stub_dir),
        "config_data": config_data if config_data is not None else CONFIG,
        "node": f"gate-{tag}",
        "write_path": f"src/{tag}.txt",
        "done_when": (
            "pytest tests/test_dispatch_returns_without_worker.py exits 0; the "
            f"stub writes marker.{tag} once its worker is spawned"
        ),
        "manifest": str(home / "manifest.md"),
        "out_path": str(home / f"driver-{tag}.json"),
    }
    payload_path = home / f"payload-{tag}.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    if hold:
        environment["RECKON_GATE_HOLD_FLAG"] = str(home / "supervisor-started")
        environment["RECKON_GATE_HOLD_SECONDS"] = str(HOLD_SECONDS)
    stdout = (home / f"driver-{tag}.out").open("w")
    stderr = (home / f"driver-{tag}.err").open("w")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            [str(PYTHON), str(host["driver"]), str(payload_path)],
            cwd=str(host["repo"]),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
        )
    finally:
        stdout.close()
        stderr.close()
    return _Run(
        tag=tag,
        home=home,
        marker_dir=marker_dir,
        process=process,
        started=started,
    )


def _wait_for(
    predicate: Callable[[], Any], *, timeout: float, interval: float = 0.05
) -> Any:
    """Return the first truthy value ``predicate`` yields, or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _reap_processes(home: Path) -> list[int]:
    """Kill every process whose environment names this case's home.

    The stub worker and the supervisor are detached by design, so neither is
    this test's child and no teardown of its own would reach them. The home is
    what every one of them carries and nothing else on the host does.
    """
    needle = str(home).encode()
    killed = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            environ = (entry / "environ").read_bytes()
        except OSError:
            continue
        if needle not in environ:
            continue
        pid = int(entry.name)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
        killed.append(pid)
    return sorted(killed)


def _finish(run: _Run, outcome: dict[str, Any]) -> dict[str, Any]:
    """End the case: reap what it started and record what it left."""
    outcome["pointer_at_end"] = run.pointer
    if run.process.poll() is None:
        run.process.kill()
        run.process.wait(timeout=30)
    outcome["dispatch_returncode"] = run.process.returncode
    outcome["reaped_processes"] = run.survivors()
    outcome["own_pointer_names"] = sorted(run.own_pointer_names())
    return outcome


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "entries": []}
    return {"exists": True, "entries": sorted(item.name for item in path.iterdir())}


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


# --------------------------------------------------------------------------
# The measured reference the exit bound is derived from.
# --------------------------------------------------------------------------


def _measured_reference_seconds(host: dict[str, Any]) -> float:
    """Time fresh interpreters importing the package under test, here and now.

    Each unit is one interpreter startup that imports the package under test,
    which is the class of process every repository-facing step of dispatch's
    pre-return path starts and therefore the unit its cost is paid in on this
    host. The slowest of ``REFERENCE_SAMPLES`` samples is returned, so one
    lucky sample cannot set the bound below what the host is charging while the
    dispatch runs, and the value is taken beside the dispatch rather than from
    an earlier session on another machine.
    """
    slowest = 0.0
    for _ in range(REFERENCE_SAMPLES):
        started = time.monotonic()
        subprocess.run(
            [str(PYTHON), "-c", REFERENCE_IMPORT],
            cwd=str(host["base"]),
            env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)},
            check=True,
            capture_output=True,
        )
        slowest = max(slowest, time.monotonic() - started)
    return slowest


def _survival_window_seconds() -> float:
    """The bounded wait dispatch itself is designed to spend before returning.

    Read from the module under test rather than restated, so the bound follows
    the window it is built on if that window changes.
    """
    return float(dispatch_module.SUPERVISOR_SURVIVAL_SECONDS)


def _dispatch_exit_bound(reference_seconds: float) -> float:
    """Dispatch's own-clock bound: its survival window plus measured units.

    The window is the bounded wait ``_confirm_supervisor_survived`` performs
    while the supervisor it started is alive and has not yet recorded a worker;
    the rest is REFERENCE_UNITS units of the reference round trip the case
    measured on this host at this moment.
    """
    return _survival_window_seconds() + REFERENCE_UNITS * reference_seconds


# --------------------------------------------------------------------------
# The cases.
# --------------------------------------------------------------------------


def test_dispatch_returns_once_its_supervisor_runs(
    host: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    writable_marker_dir: Path,
) -> None:
    """Case one: a return bounded from a reference measured on this host, and a
    scan held open across it.

    The stub writes its marker at once, so the only thing between dispatch's
    start and the marker is the supervisor's own work. The shim holds the
    supervisor's boundary scan until this case releases it, so the return bound
    is measured against a scan that is still in progress rather than against a
    fixed wait for a slow scan to finish. The bound itself is not fixed: a
    reference round trip is measured on this host immediately before the
    dispatch, and dispatch's clock is bounded by its survival window plus
    REFERENCE_UNITS of those units, so a loaded host widens the bound with the
    load instead of failing on a constant chosen on a quiet day.
    """
    marker_dir = writable_marker_dir
    real_before = _snapshot(REAL_LIVE_DIR)
    reference_seconds = _measured_reference_seconds(host)
    run = _start_dispatch(
        host,
        tag="returns",
        marker_dir=marker_dir,
        stub_sleep=0,
        hold=False,
        scan_gate=True,
    )
    outcome: dict[str, Any] = {"case": "dispatch returns once its supervisor runs"}
    exit_after = None
    try:
        run.process.wait(timeout=SPAWN_BOUND)
        exit_after = run.elapsed()
        output = run.output()
        outcome["dispatch_process_seconds"] = round(exit_after, 3)
        outcome["driver_output"] = output
        outcome["driver_stderr_tail"] = run.stderr()
        assert run.process.returncode == 0, (
            "the dispatch process failed before returning: "
            f"{outcome['driver_stderr_tail']!r}"
        )
        assert output is not None, (
            "the dispatch process returned no JSON, so it produced no run id"
        )
        outcome["driver_process_pid"] = run.process.pid
        startup = float(output.get("startup_seconds") or 0.0)
        outcome["driver_startup_seconds"] = round(startup, 3)
        # Two readings of the same quantity, the driver's own import timed
        # inside the process that pays it and the case's reference round trips
        # beside it; the slower one sets the bound, so neither a lucky spawn in
        # the case nor a warm import cache in the driver can hold the bound
        # under what this host is charging while the dispatch runs.
        reference = max(reference_seconds, startup)
        bound = _dispatch_exit_bound(reference)
        outcome["reference_seconds"] = round(reference, 3)
        outcome["dispatch_exit_bound"] = round(bound, 3)
        assert exit_after <= bound + startup + DRIVER_EXIT_SLACK, (
            f"the dispatch process was alive {exit_after:.3f} s after its start, "
            f"{startup:.3f} s of it importing the package under test, past the "
            f"{bound:.3f} s bound derived here from the {reference:.3f} s "
            "reference measured on this host at this moment, so it waited on "
            "something that belongs to the supervisor"
        )
        outcome["dispatch_seconds"] = round(float(output["dispatch_seconds"]), 3)
        assert float(output["dispatch_seconds"]) <= bound, (
            f"dispatch itself took {output['dispatch_seconds']:.3f} s, past its "
            f"{bound:.3f} s bound derived here from the {reference:.3f} s "
            "reference measured on this host at this moment, so it waited on "
            "something that belongs to the supervisor"
        )
        assert output.get("run_id"), "the dispatch process reported no run id"
        assert str(output.get("reckon_file", "")).startswith(str(PACKAGE_ROOT)), (
            f"the driver imported {output.get('reckon_file')!r}, which is not the "
            f"package under test at {PACKAGE_ROOT}, so this case measured the "
            "wrong tree"
        )
        run.pointer = run.read_pointer()
        assert run.pointer, "the dispatch process published no pointer"
        assert run.pointer.get("run_id") == output["run_id"], (
            f"the pointer names run {run.pointer.get('run_id')!r} while the "
            f"dispatch process reported {output['run_id']!r}"
        )
        assert run.pointer.get("pid"), (
            "the pointer names no process, so the run has no supervisor identity"
        )
        supervisor_pid = int(run.pointer["pid"])
        outcome["supervisor_pid"] = supervisor_pid
        assert _process_alive(supervisor_pid), (
            "the pointer's process is not running, so nothing will take the "
            "boundary or spawn the worker"
        )

        # The scan signals from the first charged call it makes under the
        # worktree root and is held there. Awaiting that signal proves the scan
        # is in progress; the worker is spawned only after the scan completes,
        # so it cannot have run while the scan is held.
        signalled = _wait_for(run.scan_started, timeout=SPAWN_BOUND)
        outcome["scan_signalled"] = bool(signalled)
        assert signalled, (
            f"the boundary scan signalled no progress signal within {SPAWN_BOUND} "
            "s, so dispatch's return was not measured against a scan this case "
            f"holds open. driver stderr: {outcome['driver_stderr_tail']!r}"
        )
        assert not run.marker.exists(), (
            "the stub's marker already existed while the boundary scan was still "
            "held, so the worker was spawned without waiting for the scan the "
            "shim holds open"
        )

        # Release the held scan; the supervisor finishes it, takes the boundary,
        # and spawns the worker.
        run.release_scan()
        marker = _wait_for(run.marker_present, timeout=MARKER_BOUND)
        outcome["marker_after_seconds"] = round(run.elapsed(), 3)
        assert marker, (
            f"the stub wrote no marker within {MARKER_BOUND} s of dispatch's "
            "start, so the supervisor neither spawned nor kept a worker. driver "
            f"stderr: {outcome['driver_stderr_tail']!r}"
        )

        # The pre-return path's own charged calls: the shim records the pid that
        # ran each one, which is what separates dispatch's calls from the scan's
        # while the two run concurrently.
        lines = run.shim_lines()
        pre_return = [line for line in lines if line["pid"] == str(run.process.pid)]
        outcome["pre_return_status_calls"] = pre_return
        outcome["status_calls_total"] = len(lines)
        assert len(pre_return) <= PRE_RETURN_STATUS_BOUND, (
            f"dispatch's own pre-return path made {len(pre_return)} charged status "
            f"calls, past the declared bound of {PRE_RETURN_STATUS_BOUND}: "
            f"{pre_return}"
        )

        # The boundary snapshot is written by the supervisor, into the run
        # directory, after the scan it exists to bound.
        snapshot_path = run.run_directory() / "tree-snapshot.json"
        outcome["tree_snapshot_path"] = str(snapshot_path)
        snapshot = _wait_for(lambda: _load_json(snapshot_path), timeout=MARKER_BOUND)
        assert snapshot is not None, (
            f"no readable tree snapshot appeared at {snapshot_path} within "
            f"{MARKER_BOUND} s, so the supervisor never took the boundary"
        )
        # The snapshot is the two-tree scan a fenced supervisor takes: the run's
        # own worktree and the main checkout, and none of the thirty registered
        # worktrees an unfenced scan would cross. The charged calls, separated by
        # pid, name the same two trees and no more.
        trees = [str(tree.get("path") or "") for tree in snapshot.get("trees") or ()]
        own_tree = Path(str((run.pointer or {}).get("worktree") or "")).resolve()
        outcome["tree_snapshot_tree_count"] = len(trees)
        outcome["own_worktree"] = str(own_tree)
        assert {Path(tree).resolve() for tree in trees} == {
            host["repo"].resolve(),
            own_tree,
        }, (
            f"the snapshot holds {trees!r}, not the run's own worktree {own_tree} "
            f"and the main checkout {host['repo']}, so this is not a fenced run's "
            "two-tree scan"
        )
        scan_calls = [line for line in lines if line["pid"] == str(supervisor_pid)]
        outcome["supervisor_scan_calls"] = scan_calls
        assert {Path(line["cwd"]).resolve() for line in scan_calls} == {
            host["repo"].resolve(),
            own_tree,
        }, (
            f"the supervisor's charged status calls cover {scan_calls!r}, not "
            f"exactly {own_tree} and {host['repo']}, so the scan read a tree a "
            "fenced run's boundary check must not read"
        )

        # The promotion check reads that file. Proven by capturing the roots it
        # passes to the scan it re-runs: without the run-directory read the
        # record carries no snapshot and the check returns before any scan.
        captured: dict[str, Any] = {}

        class _ReachedError(Exception):
            pass

        def _capture(repository: Path, *, roots: Any = None) -> Any:
            captured["roots"] = list(roots or ())
            captured["repository"] = str(repository)
            raise _ReachedError

        record = {
            "run_id": output["run_id"],
            "project": PROJECT,
            "repo": str(host["repo"]),
            "worktree": str(host["repo"]),
            "node": {"write_paths": []},
        }
        monkeypatch.setenv("RECKON_HOME", str(run.home))
        monkeypatch.setattr(promotion_scope, "_repository_tree_snapshot", _capture)
        with pytest.raises(_ReachedError):
            promotion._repository_tree_boundary_violations(
                str(output["run_id"]), record
            )
        outcome["promotion_roots"] = captured.get("roots")
        assert captured.get("roots") == trees, (
            "the promotion check did not read the run directory's snapshot: it "
            f"scanned {captured.get('roots')!r} where the snapshot names {trees!r}"
        )
    finally:
        # Release any scan still held, so a case that failed before its release
        # does not leave the shim waiting out its own bound.
        run.release_scan()
        _finish(run, outcome)
        host["outcomes"].append(outcome)

    real_after = _snapshot(REAL_LIVE_DIR)
    added = [
        name for name in real_after["entries"] if name not in real_before["entries"]
    ]
    # The real live directory is shared with every other session on this
    # workstation, so an entry that appears while this case runs is not
    # necessarily this case's. What must hold is narrower and checkable: none of
    # the pointers this case created may be in it. A peer's concurrent dispatch
    # is not this case's leak.
    own_pointer_names = run.own_pointer_names()
    leaked = sorted(name for name in added if name in own_pointer_names)
    outcome["real_pointer_directory"] = {
        "path": str(REAL_LIVE_DIR),
        "before": real_before["entries"],
        "after": real_after["entries"],
        "added": added,
        "own_pointer_names": sorted(own_pointer_names),
        "leaked": leaked,
    }
    assert not leaked, (
        f"this case wrote its own run into the real fleet's live-pointer "
        f"directory: {leaked}"
    )


def test_sigkill_to_dispatch_leaves_the_worker_marker(
    host: dict[str, Any], tmp_path: Path, writable_marker_dir: Path
) -> None:
    """Case two: the dispatch process dies mid-run; its worker does not.

    The driver is parked inside ``crew.dispatch`` by the sitecustomize hook the
    moment its supervisor exists, so the kill lands while dispatch is still in
    its own launch path rather than after it returned.
    """
    marker_dir = writable_marker_dir
    run = _start_dispatch(
        host, tag="killed-dispatch", marker_dir=marker_dir, stub_sleep=0, hold=True
    )
    outcome: dict[str, Any] = {
        "case": "sigkill to the dispatch process before it returns"
    }
    try:
        flag = run.home / "supervisor-started"
        parked = _wait_for(
            lambda: flag.exists() and flag.read_text().strip(), timeout=SPAWN_BOUND
        )
        assert parked, (
            f"the dispatch process never reached its supervisor within "
            f"{SPAWN_BOUND} s, so no kill could be delivered. driver stderr: "
            f"{run.stderr()!r}"
        )
        outcome["driver_was_parked_seconds"] = round(run.elapsed(), 3)
        assert run.process.poll() is None, (
            "the dispatch process had already returned before the kill, so this "
            "case would measure a kill delivered after the return rather than "
            "before it"
        )
        supervisor_pid = int(parked.strip())
        run.pointer = run.read_pointer()
        assert run.pointer, "the dispatch process published no pointer for its run"
        outcome["supervisor_pid"] = supervisor_pid
        outcome["pointer_pid_while_parked"] = run.pointer.get("pid")
        assert _process_alive(supervisor_pid), (
            "the supervisor the dispatch process just started is not running, so "
            "the process that must survive the kill does not exist"
        )
        os.kill(run.process.pid, signal.SIGKILL)
        run.process.wait(timeout=30)
        outcome["dispatch_returncode"] = run.process.returncode
        assert run.process.returncode == -signal.SIGKILL, (
            f"the dispatch process ended with {run.process.returncode}, not "
            "SIGKILL, so the kill was not the cause of its end"
        )
        marker = _wait_for(run.marker_present, timeout=MARKER_BOUND)
        outcome["marker_after_seconds"] = round(run.elapsed(), 3)
        outcome["supervisor_alive_after_kill"] = _process_alive(supervisor_pid)
        assert marker, (
            "the stub wrote no marker after the dispatch process was killed, so "
            "the worker depended on the process that launched its supervisor. "
            f"driver stderr: {run.stderr()!r}"
        )
    finally:
        _finish(run, outcome)
        host["outcomes"].append(outcome)


def test_sigkill_to_the_worker_is_recorded(
    host: dict[str, Any], tmp_path: Path, writable_marker_dir: Path
) -> None:
    """Case three: a worker killed by signal has its signal recorded."""
    marker_dir = writable_marker_dir
    run = _start_dispatch(
        host, tag="killed-worker", marker_dir=marker_dir, stub_sleep=90, hold=False
    )
    outcome: dict[str, Any] = {"case": "sigkill to the worker"}
    try:
        run.process.wait(timeout=SPAWN_BOUND)
        assert run.process.returncode == 0, (
            f"the dispatch process failed: {run.stderr()!r}"
        )
        run.pointer = run.read_pointer()
        output = run.output() or {}
        assert output.get("run_id"), "the dispatch process returned no run id"
        run_directory = run.run_directory()
        worker_record = _wait_for(
            lambda: _load_json(run_directory / "worker.json"), timeout=SPAWN_BOUND
        )
        assert worker_record is not None, (
            f"the supervisor wrote no worker.json within {SPAWN_BOUND} s, so "
            "there was no worker to kill"
        )
        worker_pid = int(worker_record["pid"])
        outcome["worker_pid"] = worker_pid
        assert _process_alive(worker_pid), (
            "the worker named on the record is not running"
        )
        os.kill(worker_pid, signal.SIGKILL)
        exit_record = _wait_for(
            lambda: _load_json(run_directory / "exit.json"), timeout=EXIT_RECORD_BOUND
        )
        outcome["exit_record"] = exit_record
        assert exit_record is not None, (
            f"no exit.json appeared within {EXIT_RECORD_BOUND} s of the worker "
            "receiving SIGKILL, so nothing collected the exit the signal caused"
        )
        assert exit_record.get("signal_name") == "SIGKILL", (
            f"the exit record names {exit_record.get('signal_name')!r} rather "
            "than SIGKILL"
        )
        assert exit_record.get("worker_pid") == worker_pid
        assert exit_record.get("exit_code") is None
        assert exit_record.get("recorded_by") == "supervisor"
        assert not run.marker.exists(), (
            "the stub wrote its marker, so it was not killed before it finished "
            "and the record cannot be the signal's"
        )
    finally:
        _finish(run, outcome)
        host["outcomes"].append(outcome)


def test_a_discarded_run_is_not_recreated(
    host: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    writable_marker_dir: Path,
) -> None:
    """Case four: a discard during a live worker leaves nothing behind."""
    _assert_phase_publish_cannot_restore_released_claim(
        tmp_path / "release-race", monkeypatch
    )
    marker_dir = writable_marker_dir
    run = _start_dispatch(
        host,
        tag=f"discarded-{uuid.uuid4().hex[:8]}",
        marker_dir=marker_dir,
        stub_sleep=5,
        hold=False,
    )
    outcome: dict[str, Any] = {"case": "a discarded run is not recreated"}
    try:
        run.process.wait(timeout=SPAWN_BOUND)
        assert run.process.returncode == 0, (
            f"the dispatch process failed: {run.stderr()!r}"
        )
        run.pointer = run.read_pointer()
        output = run.output() or {}
        assert output.get("run_id"), "the dispatch process returned no run id"
        run_directory = run.run_directory()
        pointer = run.pointer_path()
        # Read from the record dispatch returned, not from the live pointer:
        # the supervisor's own phase publishes are a concurrent writer of that
        # pointer and can carry back the pid-less record they read.
        supervisor_pid = int(output.get("pid") or 0)
        assert supervisor_pid, "the dispatch process named no supervisor to wait for"

        worker_record = _wait_for(
            lambda: _load_json(run_directory / "worker.json"), timeout=SPAWN_BOUND
        )
        assert worker_record is not None, (
            f"the supervisor wrote no worker.json within {SPAWN_BOUND} s, so "
            "there was no live worker to discard out from under"
        )
        assert _process_alive(int(worker_record["pid"])), (
            "the worker had already exited, so this case would not discard a run "
            "whose worker was still alive"
        )

        monkeypatch.setenv("RECKON_HOME", str(run.home))
        dispatch_module._release_launch_claim(str(output["run_id"]))
        outcome["discarded_at_seconds"] = round(run.elapsed(), 3)
        assert not pointer.exists() and not run_directory.exists(), (
            "the discard did not remove the pointer and the run directory"
        )
        outcome["discarded_while_worker_alive"] = _process_alive(
            int(worker_record["pid"])
        )

        # The discard takes the bookkeeping, not the worker: the stub still runs
        # to completion. That is the positive control for the assertion below —
        # a supervisor whose worker never finished would leave no reason for it
        # to have made the write that must be dropped.
        marker = _wait_for(run.marker_present, timeout=MARKER_BOUND)
        outcome["worker_completed_after_discard"] = bool(marker)
        assert marker, (
            f"the worker wrote no marker within {MARKER_BOUND} s of the discard, "
            "so it was killed by the discard rather than finishing after it"
        )

        # The supervisor's final write is what must be dropped, and it ends once
        # it has attempted that write: the process going away is the completion
        # signal, so waiting on it avoids asserting against a supervisor still
        # to make its last attempt.
        finished = _wait_for(
            lambda: not _process_alive(supervisor_pid), timeout=DISCARD_BOUND
        )
        outcome["supervisor_finished_after_discard"] = bool(finished)
        assert finished, (
            f"the supervisor was still running {DISCARD_BOUND} s after its run "
            "was discarded, so it neither finished nor recorded anything"
        )
        outcome["pointer_exists_after"] = pointer.exists()
        outcome["run_directory_exists_after"] = run_directory.exists()
        assert not run_directory.exists(), (
            "the supervisor recreated its run directory after the run was "
            "discarded, so a discarded run comes back into existence"
        )
        assert not pointer.exists(), (
            "the supervisor wrote a pointer for a run that was discarded"
        )
    finally:
        _finish(run, outcome)
        host["outcomes"].append(outcome)


def test_a_phase_publish_does_not_restore_a_released_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_phase_publish_cannot_restore_released_claim(tmp_path, monkeypatch)


def _assert_phase_publish_cannot_restore_released_claim(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "discarded-publish"
    with monkeypatch.context() as isolated:
        isolated.setenv("RECKON_HOME", str(home))
        run_directory = dispatch_module.run_dir(run_id)
        run_directory.mkdir(parents=True)
        pointer = dispatch_module.pointer_path(run_id)
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({"run_id": run_id, "phase": "starting"}))
        dispatch_picker_module = importlib.import_module("reckon.crew.dispatch_picker")
        original_write = dispatch_picker_module._write_json
        original_remove = dispatch_module.shutil.rmtree
        write_started = Event()
        allow_write = Event()
        removal_started = Event()
        allow_removal = Event()

        def delayed_write(path: Path, payload: dict[str, Any]) -> None:
            write_started.set()
            assert allow_write.wait(10)
            original_write(path, payload)

        def delayed_remove(path: Path, *args: Any, **kwargs: Any) -> None:
            removal_started.set()
            assert allow_removal.wait(10)
            original_remove(path, *args, **kwargs)

        isolated.setattr(dispatch_picker_module, "_write_json", delayed_write)
        isolated.setattr(dispatch_module.shutil, "rmtree", delayed_remove)
        with ThreadPoolExecutor(max_workers=2) as pool:
            publish = pool.submit(
                dispatch_module._publish_stored_phase, {"run_id": run_id}, ended=False
            )
            try:
                assert write_started.wait(10), (
                    "the phase publish did not reach its write"
                )
                release = pool.submit(dispatch_module._release_launch_claim, run_id)
                removal_started.wait(2)
                allow_write.set()
                publish.result(timeout=10)
                allow_removal.set()
                release.result(timeout=10)
            finally:
                allow_write.set()
                allow_removal.set()
        assert not pointer.exists(), "the publish recreated the released pointer"


def test_a_delegated_launch_records_a_boundary_snapshot(
    host: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    writable_marker_dir: Path,
) -> None:
    """Case five: a lane that spawns nothing still takes the boundary baseline.

    A delegated launch hands the run to the calling harness, so no supervisor
    stands between dispatch and the worker. Dispatch must therefore take the
    baseline itself, and it must land where the promotion check reads it — a run
    directory snapshot — or the refusal below never fires for this lane and a
    stray uncommitted edit in another tree lands unremarked.
    """
    tag = "delegated"
    declared = f"src/{tag}.txt"
    marker_dir = writable_marker_dir
    run = _start_dispatch(
        host,
        tag=tag,
        marker_dir=marker_dir,
        stub_sleep=0,
        hold=False,
        config_data=IN_HARNESS_CONFIG,
    )
    outcome: dict[str, Any] = {
        "case": "a delegated launch records the boundary snapshot inline"
    }
    stray = host["repo"] / declared
    try:
        run.process.wait(timeout=SPAWN_BOUND)
        output = run.output()
        outcome["driver_output"] = output
        outcome["driver_stderr_tail"] = run.stderr()
        assert run.process.returncode == 0, (
            f"the delegated dispatch failed: {outcome['driver_stderr_tail']!r}"
        )
        assert output is not None and output.get("run_id"), (
            "the delegated dispatch returned no run id"
        )
        assert output.get("pid") is None, (
            f"the delegated launch named process {output.get('pid')!r}, so it "
            "spawned one and this case no longer measures the lane that spawns "
            "nothing"
        )
        run.pointer = run.read_pointer()
        assert run.pointer, "the delegated dispatch published no pointer"
        assert run.pointer.get("launch") == "in-harness", (
            f"the pointer records launch {run.pointer.get('launch')!r}, so this "
            "case did not measure a delegated launch"
        )
        run_id = str(output["run_id"])

        # The baseline itself: written by dispatch, into the run directory,
        # where the promotion check reads it.
        snapshot_path = run.run_directory() / "tree-snapshot.json"
        outcome["tree_snapshot_path"] = str(snapshot_path)
        snapshot = _load_json(snapshot_path)
        assert isinstance(snapshot, dict), (
            f"the delegated launch wrote no readable boundary snapshot at "
            f"{snapshot_path}, so this lane has no baseline for the promotion "
            "check to compare against"
        )
        trees = [str(tree.get("path") or "") for tree in snapshot.get("trees") or ()]
        outcome["tree_snapshot_tree_count"] = len(trees)
        assert len(trees) >= WORKTREE_COUNT, (
            f"the snapshot holds {len(trees)} trees, fewer than the "
            f"{WORKTREE_COUNT} worktrees the scan had to cross"
        )

        record = {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(host["repo"]),
            "worktree": str(run.pointer.get("worktree") or ""),
            "node": {"write_paths": [declared]},
        }
        monkeypatch.setenv("RECKON_HOME", str(run.home))

        # The baseline is clean: the last write this repository saw is dispatch's
        # own member-registration commit, and it is behind the snapshot. A check
        # that reported a violation here would make the one below prove nothing.
        clean = promotion._repository_tree_boundary_violations(run_id, record)
        outcome["violations_before_the_edit"] = clean
        assert clean == [], (
            "the boundary check already reported a violation before any edit "
            f"was made, so a violation after it would prove nothing: {clean}"
        )

        # The stray edit: an untracked file at the run's declared path, in the
        # main checkout rather than in the run's own worktree.
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text("stray\n", encoding="utf-8")
        violations = promotion._repository_tree_boundary_violations(run_id, record)
        outcome["violations_after_the_edit"] = violations
        assert violations, (
            "a stray uncommitted edit at the run's declared path in the main "
            "checkout was not reported, so the boundary check is not reading "
            "this lane's snapshot"
        )
        assert any(declared in item for item in violations), (
            f"the violation does not name the stray path {declared!r}: {violations}"
        )
        assert any("main checkout" in item for item in violations), (
            f"the violation does not name the tree that holds the edit: {violations}"
        )

        # And the refusal a promotion would hit, rather than the predicate alone.
        with pytest.raises(CrewError) as refusal:
            promotion._require_repository_tree_boundary(run_id, record)
        outcome["refusal"] = str(refusal.value)
        assert declared in str(refusal.value) and "main checkout" in str(
            refusal.value
        ), f"the refusal does not name the stray edit: {refusal.value}"
    finally:
        if stray.exists():
            stray.unlink()
        # Only the directory this case created, and only while it is empty.
        with contextlib.suppress(OSError):
            stray.parent.rmdir()
        _finish(run, outcome)
        host["outcomes"].append(outcome)


def test_dispatch_returns_when_its_supervisor_finished_the_launch(
    host: dict[str, Any], tmp_path: Path, writable_marker_dir: Path
) -> None:
    """A supervisor that completed its launch is not read as a refusal.

    No scan gate is armed, so the supervisor scans two trees and then
    spawns the stub worker, collects its exit and writes the completion exit
    record -- possibly after dispatch's survival window. Dispatch must return that
    run rather than refuse it: a dispatch that launched a worker is never a
    refusal, whatever the worker then did.
    """
    marker_dir = writable_marker_dir
    run = _start_dispatch(
        host, tag="finished", marker_dir=marker_dir, stub_sleep=0, hold=False
    )
    outcome: dict[str, Any] = {
        "case": "a dispatch returns when its supervisor finished the launch"
    }
    try:
        run.process.wait(timeout=SPAWN_BOUND)
        output = run.output()
        outcome["driver_output"] = output
        outcome["driver_stderr_tail"] = run.stderr()
        assert run.process.returncode == 0, (
            "the dispatch process refused a launch whose supervisor reached it and "
            f"spawned its worker: {outcome['driver_stderr_tail']!r}"
        )
        assert output is not None and output.get("run_id"), (
            "the dispatch process returned no run id for a completed launch"
        )
        run.pointer = run.read_pointer()
        assert run.pointer, "the dispatch process published no pointer"
        # The supervisor's pid is taken from the record dispatch returned rather
        # than from the live pointer: the supervisor publishes its own phase by
        # read-modify-write of that same pointer, so a read taken after the
        # launch can carry back the pid-less record it read before dispatch's
        # write landed.
        assert output.get("pid"), (
            "the dispatch process named no supervisor for a launch that completed"
        )
        # The supervisor publishes exit.json by atomic rename. Watch that
        # directory before reading the receipt, then wait for the rename event.
        record_path = run.run_directory() / "exit.json"
        exit_record = _load_json(record_path)
        if exit_record is None:
            libc = ctypes.CDLL(None, use_errno=True)
            events = libc.inotify_init1(os.O_CLOEXEC)
            assert events >= 0, os.strerror(ctypes.get_errno())
            try:
                directory = os.fsencode(record_path.parent)
                watch = libc.inotify_add_watch(events, directory, 0x80)
                assert watch >= 0, os.strerror(ctypes.get_errno())
                deadline = time.monotonic() + COMPLETION_RECORD_BOUND
                while (exit_record := _load_json(record_path)) is None:
                    remaining = max(0.0, deadline - time.monotonic())
                    ready, _, _ = select.select([events], [], [], remaining)
                    assert ready and remaining > 0, (
                        f"missing exit.json at {record_path} in run directory "
                        f"{record_path.parent} after {COMPLETION_RECORD_BOUND} s"
                    )
                    os.read(events, 4096)
            finally:
                os.close(events)
        outcome["exit_record"] = exit_record
        assert exit_record is not None and exit_record.get("worker_pid") is not None, (
            "the supervisor left no completion exit record naming its worker, so "
            f"this case did not exercise the completion acceptance: {exit_record!r}"
        )
    finally:
        _finish(run, outcome)
        host["outcomes"].append(outcome)


def test_the_survival_check_refuses_a_supervisor_that_exited_without_a_worker(
    tmp_path: Path,
) -> None:
    """A supervisor with no worker receipt inside the window is still refused.

    The completion acceptance covers a supervisor that reached its run.

    The refusal is reserved for a supervisor that exited inside the window
    without a worker receipt: one that could not find its run, because it
    started under the wrong crew home, or one whose spawn failed. Either must
    still refuse, or a dispatch would report a live worker that never started.
    A real, already-exited process stands in for such a supervisor, with no
    record and then with a failure record.
    """
    directory = tmp_path / "run"
    directory.mkdir()
    run_id = "r-survival-refusal"
    ended = subprocess.Popen(["true"])
    ended.wait()
    outcome: dict[str, Any] = {
        "case": "a supervisor with no worker receipt is still refused"
    }

    with pytest.raises(CrewError) as no_record:
        dispatch_module._confirm_supervisor_survived(ended.pid, directory, run_id)
    outcome["refused_without_a_record"] = str(no_record.value)
    assert "without writing an exit record" in str(no_record.value), (
        f"a supervisor that ended with no record was not refused as one that "
        f"never reached its run: {no_record.value}"
    )

    (directory / dispatch_module.EXIT_RECORD_NAME).write_text(
        json.dumps(
            {"run_id": run_id, "worker_pid": None, "detail": "worker did not spawn"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(CrewError) as failure_record:
        dispatch_module._confirm_supervisor_survived(ended.pid, directory, run_id)
    outcome["refused_with_a_failure_record"] = str(failure_record.value)
    assert "worker did not spawn" in str(failure_record.value), (
        f"a supervisor that exited with a launch-failure record was not refused: "
        f"{failure_record.value}"
    )
