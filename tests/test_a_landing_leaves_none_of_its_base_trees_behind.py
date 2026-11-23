"""A landing measure leaves no base or reference tree behind, however it ends.

A measure releases its tree on every exit its own process reaches. A measure
whose process the kernel kills partway — a signal, or the scheduler — never
reaches that release, so its tree sits under the node-local scratch root. The
shared orphan sweep reads it but waits two hours of ctime grace before it will
act, which on a busy fleet is long enough to starve every dispatch that checks
the scratch floor. The next landing pass removes such a tree at once: the fact
that decides it is that the process which built the tree is gone.

The four exits are driven in child processes, so each reaches a real tree and
the killed one is killed with no chance to run its release. A tree a live
process still holds is kept, read from the process table the same way the
orphan sweep reads it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import landing_measure

#: The prefix a landing measure names every tree it builds with.
_BASE_TREE_PREFIX = "landing-base-"

# The four exit paths a landing measure reaches, written as a child driver that
# goes through the real checkout builder. "killed" parks inside the block so the
# parent can kill the process with SIGKILL; the others exit through the
# release the block runs, exactly as a real measure does.
_DRIVER = r"""
import sys
from pathlib import Path

from reckon.crew import landing_measure

repo, sha, outcome = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with landing_measure.base_checkout(Path(repo), sha, 30.0) as tree:
        (Path(tree) / "measure-outcome").write_text(outcome)
        if outcome == "refused":
            raise landing_measure.BaseCheckoutError("the landing refused")
        if outcome == "raises":
            raise ValueError("the measurement raised")
        if outcome == "killed":
            print("READY", flush=True)
            import time
            time.sleep(300)
except (landing_measure.BaseCheckoutError, ValueError):
    pass
"""


def _landing_trees(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        child for child in root.iterdir() if child.name.startswith(_BASE_TREE_PREFIX)
    )


def _make_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "readme.txt").write_text("base revision\n")
    subprocess.run(["git", "-C", str(path), "add", "readme.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "base"], check=True)
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, Path]:
    root = tmp_path / "scratch"
    root.mkdir()
    monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(root))
    repo = tmp_path / "repo"
    sha = _make_repo(repo)
    return repo, sha, root


def _driver_environment() -> dict[str, str]:
    env = dict(os.environ)
    package_root = Path(landing_measure.__file__).resolve().parents[2]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(package_root), existing) if part
    )
    return env


def _drive_to_exit(repo: Path, sha: str, outcome: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _DRIVER, str(repo), sha, outcome],
        env=_driver_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_a_landing_leaves_none_of_its_base_trees_behind(
    scene: tuple[Path, str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, sha, root = scene

    # The exits a measure reaches itself release their own tree.
    for outcome in ("pass", "refused", "raises"):
        completed = _drive_to_exit(repo, sha, outcome)
        assert completed.returncode == 0, completed.stderr
        assert _landing_trees(root) == [], (
            f"a {outcome} measure left a tree behind: {_landing_trees(root)}"
        )

    # A tree a live process still holds must survive the pass.
    held = root / "landing-base-held"
    held.mkdir()
    holding = (held / "held.txt").open("w")
    try:
        # A measure whose process is killed leaves its tree where it stood.
        killed_process = subprocess.Popen(
            [sys.executable, "-c", _DRIVER, str(repo), sha, "killed"],
            env=_driver_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert killed_process.stdout is not None
        ready = killed_process.stdout.readline().strip()
        assert ready == "READY", (
            killed_process.stderr.read() if killed_process.stderr else ""
        )
        before_kill = [tree for tree in _landing_trees(root) if tree != held]
        assert len(before_kill) == 1, _landing_trees(root)
        killed = before_kill[0]
        killed_process.kill()
        killed_process.wait(timeout=15)
        assert killed.exists(), "the killed measure's tree vanished without a release"

        # The next landing pass sweeps the abandoned tree and keeps the held one.
        with landing_measure.base_checkout(repo, sha, 30.0):
            pass
        reported = capsys.readouterr().err
    finally:
        holding.close()

    assert not killed.exists(), "the next landing pass left the killed tree behind"
    assert held.exists(), "the pass removed a tree a live process holds"
    assert re.search(rf"removed {re.escape(str(killed))} \(\d+ bytes\)", reported), (
        f"the removal was not reported with its path and bytes: {reported!r}"
    )
    assert _landing_trees(root) == [held], _landing_trees(root)
