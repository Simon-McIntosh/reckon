"""A review the follower's reflex launches outlives the follower's process group.

The reflex fires from the follower's cadence and hands the composed review to
``dispatch``, which starts the review's per-run supervisor. If that supervisor
stays in the follower's process tree, a Monitor stop or expiry signals the
follower's whole process group and takes the review down with it: measured twice
on this workstation, four reflex-launched reviews each dying by SIGTERM at the
second their follower's Monitor expired. A hand-dispatched review survives,
because its supervisor is detached from the launcher. The two must not differ.

So the claim is not read from source: a real follower process is started in its
own process group, its reflex really dispatches a review, and the kernel's own
record of the review's supervisor is read. The follower's whole group is then
sent SIGTERM, and the supervisor is asserted alive afterwards in a session of
its own. The negative half is asserted by the same body with the one mutation
this node declares — the launch put back in-tree — where the supervisor dies
with the group and the assertion fails, so a passing run is the detached vector
doing work rather than a group signal that reached nobody.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

dispatch_module = importlib.import_module("reckon.crew.dispatch")


def _repo_root() -> Path:
    return Path(dispatch_module.__file__).resolve().parents[2]


# The follower under measurement. It builds a throwaway project in its own
# configuration home, lets the reflex dispatch a review through the real
# dispatch path with a stub launch target standing in for the supervisor's argv,
# and prints the supervisor pid dispatch recorded for the review. It then holds
# itself open so its process group is a live target for the test's signal.
#
# The launch target is a long sleep: the vector under test is how dispatch starts
# it, not what the review then does, and the sleep keeps the supervisor alive and
# observable without a harness.
#
# ``REFLEX_LAUNCH`` selects the arm. "in-tree" is the declared mutation: it
# replaces the detaching spawn with a plain child of the follower, which is
# exactly the pre-fix behaviour the mechanism replaced. Every other value leaves
# the real vector in place.
FOLLOWER = """
import importlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

root = Path(sys.argv[1]).resolve()
home = Path(sys.argv[2]).resolve()
launch = sys.argv[3]

os.environ["RECKON_HOME"] = str(home)
os.environ["RECKON_WATCH_ARMING"] = "off"
os.environ.pop("RECKON_RUN_ID", None)
os.environ.pop("RECKON_MANIFEST", None)
sys.path.insert(0, str(root))

from reckon import crew
from reckon.crew import recovery, runs

repo = home.parent / "repo"
scripts = repo / "skills" / "reckon-build" / "scripts"
scripts.mkdir(parents=True, exist_ok=True)
(scripts / "worktree_fleet.py").write_text(
    (root / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").read_text()
)
plans = repo / "docs" / "plans"
plans.mkdir(parents=True)
(plans / "fixture.html").write_text(
    '<meta name="docs-project" content="sample">'
    '<meta name="reckon-type" content="plan">'
    '<meta name="plan-slug" content="fixture">'
    '<h2 id="s2">a review the reflex launches survives its follower</h2>',
    encoding="utf-8",
)
(repo / "seed.txt").write_text("seed\\n", encoding="utf-8")
for arguments in (
    ["init", "-q", "-b", "main"],
    ["config", "user.email", "worker@example.invalid"],
    ["config", "user.name", "Worker"],
    ["add", "seed.txt", "skills", "docs/plans/fixture.html"],
    ["commit", "-q", "-m", "chore: seed"],
):
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)
(home / "mounts.json").write_text('{"sample": "' + str(repo / "docs") + '"}')

dispatch = importlib.import_module("reckon.crew.dispatch")
base_sha = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
).stdout.strip()


def prepare_worktree(_repo, session, node, base):
    path = home.parent / "worktrees" / (session + "-" + node)
    path.mkdir(parents=True, exist_ok=True)
    return {"path": str(path), "base": base, "base_sha": base_sha}


dispatch._create_worktree = prepare_worktree
dispatch._supervisor_argv = lambda *, spec_path: [
    sys.executable,
    "-c",
    "import time; time.sleep(300)",
]
if launch == "in-tree":
    def _in_tree_spawn(argv, stderr_path):
        # No intermediate and no new session: the launch target is a plain child
        # of the follower, as the single-Popen vector was before it was fixed.
        return subprocess.Popen(argv).pid
    dispatch._spawn_detached_supervisor = _in_tree_spawn

CONFIG = {
    "default_backend": "alpha",
    "local_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "session_reuse": True,
            "time_budget": "25m",
        }
    },
    "roles": {"implement": {}, "review": {}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}

run_id = "r-work"
manifest = home / "manifests" / (run_id + ".md")
manifest.parent.mkdir(parents=True, exist_ok=True)
manifest.write_text("node: " + run_id + "\\nstatus: complete\\ncommits: " + run_id + "\\n")
crew._write_json(
    crew.pointer_path(run_id),
    {
        "run_id": run_id,
        "project": "sample",
        "repo": str(repo),
        "node": {"id": run_id, "plan": "fixture", "section": "s2"},
        "backend": "alpha",
        "launch": "cli",
        "argv": ["codex"],
        "phase": "starting",
        "process_alive": False,
        "session": "session-orchestrating",
        "manifest_path": str(manifest),
    },
)

with runs.follower_claim("sample", "session-orchestrating", delivery="stream"):
    report = recovery.dispatch_review_for_run(runs.read_pointer(run_id), config=CONFIG)

review = runs.read_pointer(str(report["review_run_id"]))
print("SUPERVISOR", int(review["pid"]), int(os.getpid()), flush=True)
time.sleep(300)
"""


def _proc_fields(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    return raw[raw.rindex(")") + 2 :].split()


def _proc_ids(pid: int) -> tuple[int, int, int]:
    """(ppid, pgrp, session) as the kernel holds them for a live pid."""
    fields = _proc_fields(pid)
    assert fields is not None, f"pid {pid} is gone"
    return int(fields[1]), int(fields[2]), int(fields[3])


def _alive(pid: int) -> bool:
    fields = _proc_fields(pid)
    return fields is not None and fields[0] != "Z"


def _wait_until(predicate, *, timeout: float = 15.0, detail: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"condition {detail or predicate} not met in {timeout:g}s")


def _launch(worktree: Path, home: Path, launch: str) -> tuple[subprocess.Popen, int]:
    """Start the follower and return it with the review supervisor's pid.

    The follower is a session and process-group leader of its own, so the group
    signal the test sends it is the follower's whole tree and nothing else.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_repo_root())
    environment.pop("RECKON_RUN_ID", None)
    environment.pop("RECKON_MANIFEST", None)
    follower = subprocess.Popen(
        [sys.executable, "-c", FOLLOWER, str(worktree), str(home), launch],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=environment,
    )
    line = follower.stdout.readline()
    assert line.startswith("SUPERVISOR"), (
        f"the follower reported no supervisor: {line!r} {follower.stderr.read()!r}"
    )
    supervisor_pid = int(line.split()[1])
    _wait_until(
        lambda: _alive(supervisor_pid), detail="the review's supervisor is alive"
    )
    return follower, supervisor_pid


def _reap(follower: subprocess.Popen, supervisor_pid: int) -> None:
    if _alive(supervisor_pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(supervisor_pid, signal.SIGKILL)
        with contextlib.suppress(AssertionError):
            _wait_until(lambda: not _alive(supervisor_pid))
    if follower.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(follower.pid, signal.SIGKILL)
        follower.wait(timeout=10)
    follower.stdout.close()
    follower.stderr.close()


def _run(tmp_path: Path, launch: str) -> None:
    """Start a follower, stop its group, and read where the review landed.

    The signal is SIGTERM to the follower's whole process group, which is what a
    Monitor stop delivers. The assertion is the kernel's own record: the review's
    supervisor is still alive, and its session is its own rather than the
    follower's — a process that stayed in the follower's group would have taken
    the same signal and be gone.
    """
    home = tmp_path / "config"
    home.mkdir()
    follower, supervisor_pid = _launch(_repo_root(), home, launch)
    try:
        follower_session = _proc_ids(follower.pid)[2]
        assert _alive(supervisor_pid)

        os.killpg(follower.pid, signal.SIGTERM)
        _wait_until(
            lambda: not _alive(follower.pid), detail="the follower's group is gone"
        )
        time.sleep(0.5)

        assert _alive(supervisor_pid), (
            "the review's supervisor died with the follower's process group"
        )
        # The survivor leads a session of its own, which is the fact that explains
        # why the group signal missed it.
        _, pgrp, session = _proc_ids(supervisor_pid)
        assert pgrp == supervisor_pid, (pgrp, supervisor_pid)
        assert session == supervisor_pid, (session, supervisor_pid)
        assert session != follower_session, (
            f"the supervisor is still in the follower's session {follower_session}"
        )
    finally:
        _reap(follower, supervisor_pid)


def test_a_reflex_review_supervisor_survives_the_followers_group(
    tmp_path: Path,
) -> None:
    """The head arm: SIGTERM to the follower's group leaves the review running."""
    _run(tmp_path, "detached")


@pytest.mark.skipif(
    os.environ.get("RECKON_REFLEX_LAUNCH") != "in-tree",
    reason="the negative-control arm runs only when the declared mutation is applied",
)
def test_a_reflex_review_supervisor_survives_the_followers_group_in_tree(
    tmp_path: Path,
) -> None:
    """The negative half: an in-tree launch dies with the group and this fails.

    Run with ``RECKON_REFLEX_LAUNCH=in-tree`` this is the declared mutation — the
    launch put back in the follower's process tree — and the survival assertion
    below it fails, which is what makes the head arm's green mean the detached
    vector did the work.
    """
    _run(tmp_path, "in-tree")
