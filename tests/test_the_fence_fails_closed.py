"""A dispatch whose fence cannot be built is refused before any worktree.

The fence is bubblewrap over a user namespace. When the binary is absent from
PATH or the kernel refuses a user namespace, the fence cannot be composed, and
a launch that proceeds without it still reports a protection it does not have.
So a cli dispatch is refused before any worktree, live pointer or run directory
exists, and ``--no-fence REASON`` is the one deliberate way through — the reason
recorded on the run, its composed plan and its ledger row.

Cases 1 to 5 drive the production dispatch entry point in a temporary
``RECKON_HOME`` with a stub launcher, so no harness runs and no worktree leaves
the scratch tree. Case 6 composes the fence for real and executes it, proving
the boundary the refusal protects; it is skipped when bubblewrap cannot run, and
the skip names the capability.

The declared negative control makes the capability check report success while
bubblewrap is absent, under which case 1's refusal does not happen and the case
fails. Running this file directly prints that mutation verbatim on the first
line and the observed outcome beneath it.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, _worker_fence, crew, ledger
from reckon.crew.runs import live_dir, pointer_path, runs_dir

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "sample"

NEGATIVE_CONTROL_MUTATION = (
    "in a scratch tree, make the fence capability check report success when "
    "bwrap is absent from PATH; case 1 then fails"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which(_backends.FENCE_BINARY) is None,
    reason=(
        "a user namespace is required to build the fence: bubblewrap is not "
        "installed, so the fence cannot run on this host"
    ),
)

PLAN_HTML = (
    "<!doctype html>\n<html><head>\n"
    f'<meta name="docs-project" content="{PROJECT}">\n'
    '<meta name="reckon-type" content="plan">\n'
    '<meta name="plan-slug" content="plan-a">\n'
    '</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>\n'
)

DISPATCH_CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "model": "some-model",
            "effort": "high",
            "sandbox": "worktree-full",
            "time_budget": "25m",
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {"implement": {}, "inline": {"backend": "native"}},
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
}


class _WorktreeReachedError(Exception):
    """Raised by the red-log driver's spy once the fence check is passed."""


class _FailedProbe:
    """A probe completing non-zero, then a plain attribute read: stand-in probe.

    The factory keeps ``stderr`` on the returned object; a class attribute is
    enough because the production code reads only ``returncode`` and ``stderr``.
    """

    returncode = 1
    stderr = "namespace creation failed: permission denied"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _build_repository(config_home: Path, root: Path) -> Path:
    """Build a synthetic repo, its mount and the fleet script; return the root.

    ``config_home`` is the temporary ``RECKON_HOME``; the caller sets the
    environment variable, so the pytest fixture and the red-log driver reuse one
    builder and cannot drift.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "plans" / "plan-a.html").write_text(PLAN_HTML, encoding="utf-8")
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    fleet_source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (scripts / "worktree_fleet.py").write_text(
        fleet_source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/plans/plan-a.html", "skills"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_home = tmp_path / "config"
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return _build_repository(config_home, tmp_path / "repo")


def _hide_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make bubblewrap vanish from PATH without disturbing any other lookup."""
    real_which = shutil.which

    def which(name: str, *args: object, **kwargs: object):
        if name == _backends.FENCE_BINARY:
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(_worker_fence.shutil, "which", which)


def _stub_launcher(plan, *, log_path, stderr_path, prompt_path) -> int:
    """Stand in for the launch: spawn nothing, return a pid."""
    return 4242


def _node(
    scratch: Path,
    node_id: str = "fence-node",
    write_paths: tuple[str, ...] = ("allowed.txt",),
) -> crew.TaskNode:
    return crew.TaskNode(
        id=node_id,
        goal="refuse a dispatch the fence cannot seal",
        plan="plan-a",
        section="§3",
        done_when="tests/test_the_fence_fails_closed.py reports its cases pass",
        write_paths=list(write_paths),
        time_budget="25m",
        manifest_path=str(scratch / "manifest.md"),
        spec_level="guided",
        role="implement",
    )


def _dispatch(
    scratch: Path,
    repository: Path,
    *,
    node_id: str = "fence-node",
    write_paths: tuple[str, ...] = ("allowed.txt",),
    **overrides,
):
    kwargs = {
        "node": _node(scratch, node_id, write_paths),
        "project": PROJECT,
        "repo": repository,
        "config": DISPATCH_CONFIG,
        "session": "fence-session",
        "check_budget": False,
        "launcher": _stub_launcher,
    }
    kwargs.update(overrides)
    return dispatch_module.dispatch(**kwargs)


def _live_pointers() -> list[Path]:
    directory = live_dir()
    return [] if not directory.is_dir() else list(directory.iterdir())


def _run_directories() -> list[Path]:
    directory = runs_dir()
    return [] if not directory.is_dir() else list(directory.iterdir())


def test_case_1_bwrap_absent_refuses_and_leaves_nothing(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hide_bwrap(monkeypatch)
    created: list[object] = []

    def spy(*args: object, **kwargs: object):
        created.append(args)
        return {"path": "", "base": "", "base_sha": ""}

    monkeypatch.setattr(dispatch_module, "_create_worktree", spy)
    before_pointers = _live_pointers()
    before_runs = _run_directories()

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(tmp_path, repository)

    message = str(refusal.value)
    assert _backends.FENCE_BINARY in message
    assert "--no-fence" in message
    # The refusal precedes the worktree: the spy never ran.
    assert created == []
    assert _live_pointers() == before_pointers
    assert _run_directories() == before_runs


def test_case_2_a_failed_namespace_probe_is_refused_naming_the_namespace(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_worker_fence, "_probe_user_namespace", _FailedProbe)

    with pytest.raises(crew.CrewError) as refusal:
        _dispatch(tmp_path, repository)

    message = str(refusal.value)
    assert "namespace" in message
    assert "permission denied" in message
    assert _live_pointers() == []


def test_case_3_no_fence_reason_launches_and_rides_the_pointer(
    repository: Path, tmp_path: Path
) -> None:
    reason = "bwrap unavailable on this sealed build host"

    record = _dispatch(tmp_path, repository, no_fence_reason=reason)

    assert record["fenced"] is False
    assert record["fence_waiver"] == {"reason": reason}
    assert _backends.FENCE_BINARY not in record["argv"]
    pointer = json.loads(pointer_path(str(record["run_id"])).read_text())
    assert pointer["fence_waiver"] == {"reason": reason}

    # The composed launch plan carries the waiver too, so a preview names it.
    plan = _backends.launch_plan(
        backend_name="alpha",
        backend=DISPATCH_CONFIG["backends"]["alpha"],
        prompt="p",
        worktree=str(tmp_path),
        fence=False,
        fence_waiver=reason,
    )
    assert plan.as_dict()["fence_waiver"] == reason


def test_case_4_promotion_writes_the_reason_onto_the_ledger_row(
    repository: Path, tmp_path: Path
) -> None:
    reason = "kernel refuses unprivileged user namespaces on this host"

    record = _dispatch(tmp_path, repository, no_fence_reason=reason)
    run_id = str(record["run_id"])
    worktree = Path(str(record["worktree"]))
    (worktree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(worktree, "add", "allowed.txt")
    _git(worktree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(worktree, "rev-parse", "HEAD")

    crew.complete(run_id, gate="passed", commits=[commit], root=repository)

    rows = ledger.runs(PROJECT, root=repository)
    row = next(item for item in rows if item.get("run_id") == run_id)
    assert row["fence_waiver"] == {"reason": reason}


@requires_bwrap
def test_case_5_an_ordinary_fenced_dispatch_is_unchanged(
    repository: Path, tmp_path: Path
) -> None:
    record = _dispatch(tmp_path, repository)

    assert record["fenced"] is True
    assert "fence_waiver" not in record
    assert record["argv"][0] == _backends.FENCE_BINARY


@requires_bwrap
def test_case_7_a_second_dispatch_does_not_reprobe(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The namespace probe forks once per process, not once per dispatch.

    Dispatch asks the capability before anything is created, so a repeat pays
    the probe's cost on its own critical path unless the verdict is memoised.
    A counting probe stands in and the two dispatches carry distinct node ids
    and write paths so the second is a real dispatch, not one refused earlier.
    """
    calls = {"n": 0}
    real_probe = _worker_fence._probe_user_namespace

    def counting():
        calls["n"] += 1
        return real_probe()

    monkeypatch.setattr(_worker_fence, "_probe_user_namespace", counting)

    _dispatch(tmp_path, repository, node_id="fence-node-a")
    _dispatch(
        tmp_path,
        repository,
        node_id="fence-node-b",
        write_paths=("allowed-b.txt",),
    )

    assert calls["n"] == 1


# ── Case 6: the fence the refusal protects, executed for real ────────────────

_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN/.own-probe" 2>/dev/null; then
  echo "own-write-ok $FENCE_OWN" >> "$FENCE_RESULTS"
else
  echo "own-write-refused $FENCE_OWN" >> "$FENCE_RESULTS"
fi
if err=$(touch "$FENCE_SIBLING/.sibling-probe" 2>&1); then
  echo "sibling-write-landed $FENCE_SIBLING" >> "$FENCE_RESULTS"
else
  echo "sibling-write-refused $FENCE_SIBLING :: $err" >> "$FENCE_RESULTS"
fi
"""


class _Pool:
    """A synthetic home: one checkout and two sibling worktrees under the pool."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.config = self.home / ".config" / "reckon"
        self.run = self.config / "crew" / "runs" / "r1"
        self.run.mkdir(parents=True)
        self.checkout = self.home / "Code" / "proj"
        self.checkout.mkdir(parents=True)
        _git(self.checkout, "init", "-q")
        _git(self.checkout, "config", "user.email", "t@example.com")
        _git(self.checkout, "config", "user.name", "t")
        _git(self.checkout, "config", "commit.gpgsign", "false")
        (self.checkout / "README").write_text("x")
        self.pool = self.home / "Code" / ".reckon-worktrees"
        self.pool.mkdir(parents=True)
        _git(self.checkout, "add", "README")
        _git(self.checkout, "commit", "-q", "-m", "init")
        self.own = self.pool / "session-a" / "node-a"
        self.sibling = self.pool / "session-b" / "node-b"
        _git(self.checkout, "worktree", "add", "--detach", str(self.own))
        _git(self.checkout, "worktree", "add", "--detach", str(self.sibling))
        self.stub = tmp_path / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)
        self.results = self.run / "results.txt"

    def argv(self) -> list[str]:
        """Compose the launch the way a dispatch does, through the production path."""
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": str(self.stub),
                "dialect": "claude",
                "sandbox": _backends.WORKTREE_FULL,
            },
            prompt="p",
            worktree=str(self.own),
            manifest_path=str(self.run / "manifest.md"),
            writable_directories=[str(self.run)],
            fence=True,
            fence_home=str(self.home),
        ).argv

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_OWN": str(self.own),
            "FENCE_SIBLING": str(self.sibling),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.own),
            capture_output=True,
            text=True,
            check=False,
        )


@requires_bwrap
def test_case_6_a_fenced_stub_writes_its_own_tree_and_not_a_sibling(
    tmp_path: Path,
) -> None:
    pool = _Pool(tmp_path)
    proc = pool.run_fence(pool.argv())

    assert proc.returncode == 0, proc.stderr
    results = pool.results.read_text()
    assert "own-write-ok" in results, results
    assert (pool.own / ".own-probe").exists()
    assert f"sibling-write-refused {pool.sibling}" in results
    assert "Read-only file system" in results
    assert not (pool.sibling / ".sibling-probe").exists()


# ── The declared negative control ────────────────────────────────────────────


def _negative_control_report(root: Path) -> tuple[int, list[str]]:
    """Run case 1 with the capability check forced to succeed; report the lines.

    Under the mutation the fence is deemed buildable while bubblewrap is absent,
    so the dispatch is not refused: it passes the check and reaches the worktree
    spy, which raises to stop it there. Case 1 has then failed. The return code
    is 1 whether the case failed as expected or unexpectedly refused anyway, so
    the log is red either way; the printed lines name which happened.
    """
    lines: list[str] = []
    os.environ["RECKON_HOME"] = str(root / "config")
    built = _build_repository(root / "config", root / "repo")
    real_which = shutil.which
    real_problem = _backends.fence_capability_problem

    def which(name: str, *args: object, **kwargs: object):
        if name == _backends.FENCE_BINARY:
            return None
        return real_which(name, *args, **kwargs)

    def reached(*args: object, **kwargs: object):
        raise _WorktreeReachedError()

    # The mutation: the capability check reports success while bwrap is absent.
    _worker_fence.shutil.which = which
    _backends.fence_capability_problem = lambda *a, **k: None
    dispatch_module._create_worktree = reached
    try:
        try:
            _dispatch(root, built)
        except crew.CrewError as exc:
            lines.append(f"case 1 refused under the mutation: {exc}")
            lines.append(
                "case 1 did NOT fail: forcing the check to succeed still left a "
                "fence refusal in place, so the mutation is not the guard"
            )
            return 1, lines
        except _WorktreeReachedError:
            lines.append(
                "case 1 FAILED as declared: with the check forced to succeed the "
                "dispatch passed the fence check and reached the worktree"
            )
            return 1, lines
        lines.append("the dispatch neither refused nor reached the worktree")
        return 1, lines
    finally:
        _worker_fence.shutil.which = real_which
        _backends.fence_capability_problem = real_problem
        os.environ.pop("RECKON_HOME", None)


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    for name in ("RECKON_RUN_ID", "RECKON_MANIFEST", "RECKON_ATTEMPT_STARTED_AT"):
        os.environ.pop(name, None)
    with tempfile.TemporaryDirectory() as directory:
        code, report = _negative_control_report(Path(directory))
        for line in report:
            print(line)
    print(f"EXIT={code}")
    sys.exit(code)
