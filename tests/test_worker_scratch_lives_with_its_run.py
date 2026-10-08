"""A worker's scratch directory lives and dies with its run.

The host's temp root is node-local and shared by every session on the machine,
so a run that writes there leaves entries nothing owns and nothing removes. Each
run is handed a private directory beneath a reckon-owned root, named for its run
id, and pointed at it by ``TMPDIR``; promotion and discard remove that directory
so it cannot outlive the run.

Five properties, and the fourth and fifth are what make the first three mean
anything:

* the environment a dispatched worker is launched with carries a ``TMPDIR`` that
  is the run's own scratch directory, beneath the node-local scratch root;
* a promoted run's scratch directory is removed, and the removal is reported;
* a scratch directory a run's own record names beneath the current root is
  removed, and the removal is reported;
* a scratch directory that belongs to a *different* run survives both;
* a record made under a scratch root that has since moved is withheld and left
  in place, never removed.

Everything is synthesised under ``tmp_path`` and the scratch root is redirected
by ``RECKON_WORKER_SCRATCH_ROOT``, so no test reads or writes the host's real
temp directory.

Running this file directly reproduces the red log: its first line is the
declared mutation, verbatim, and what follows is the observed refusal.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import reckon.crew.dispatch_launch as dispatch_launch_module
from reckon import _plan_html, crew
from reckon.crew import promotion
from reckon.crew.runs import _write_json, pointer_path

# `reckon.crew.dispatch` is shadowed by the `dispatch` function the package
# re-exports, so the module is reached by importlib rather than by attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"
PLAN = "plan-a"

# The declared mutation: with this set, promotion and discard leave the run's
# scratch directory in place, so each removal measure must fail on the
# surviving directory.
NEGATIVE_CONTROL_MUTATION = (
    "leave the per-run scratch directory in place at promotion and discard"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_WORKER_SCRATCH_NEGATIVE") == "1"

# The second declared mutation: trust a recorded scratch path without checking
# it, so a record naming a different run's directory removes that directory.
RECORDED_CONTROL_MUTATION = "trust the recorded scratch path without checking it"
RECORDED_CONTROL = os.environ.get("RECKON_WORKER_SCRATCH_RECORDED_NEGATIVE") == "1"

# The third declared mutation: accept a recorded path whose parent is not the
# current scratch root, so a record made under a root that has since moved
# removes the directory it names instead of leaving it in place.
MOVED_ROOT_CONTROL_MUTATION = (
    "accept a recorded path whose parent is not the current scratch root"
)
MOVED_ROOT_CONTROL = os.environ.get("RECKON_WORKER_SCRATCH_MOVED_NEGATIVE") == "1"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_resource(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    bare = (
        "<!doctype html><html><head>"
        f'<meta name="docs-project" content="{PROJECT}">'
        f"<title>{state['slug']}</title>"
        '</head><body><main class="plan-doc"></main></body></html>\n'
    )
    path.write_text(_plan_html.write_state(bare, state), encoding="utf-8")


@pytest.fixture()
def scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch root under the test's temp directory, never the host's."""
    root = tmp_path / "scratch"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(root))
    return root


@pytest.fixture()
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real repository with its crew directories under a temporary home."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repo = tmp_path / "repository"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "docs" / "state" / PROJECT).mkdir(parents=True)
    _write_resource(
        repo / "docs" / "plans" / f"{PLAN}.html",
        {
            "type": "plan",
            "slug": PLAN,
            "title": "Plan A",
            "status": "active",
            "version": 0,
            "comments": {},
        },
    )
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt", "docs")
    _git(repo, "commit", "-q", "-m", "seed repository")
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repo / "docs")}), encoding="utf-8"
    )
    return repo


def _worktree(repository: Path, tmp_path: Path, name: Path | str) -> Path:
    worktree = tmp_path / "worktrees" / str(name)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    return worktree


def _pointer(
    run_id: str,
    *,
    repository: Path,
    worktree: Path,
    launch: str = "in-harness",
) -> None:
    try:
        base_sha = _git(repository, "rev-parse", "HEAD")
    except (subprocess.CalledProcessError, OSError):
        base_sha = ""
    crew.run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _write_json(
        pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "repo": str(repository),
            "worktree": str(worktree),
            "launch": launch,
            "role": "implement",
            "created_at": "2026-09-28T14:00:00Z",
            "base_sha": base_sha,
            "scratch": str(dispatch.worker_scratch_dir(run_id)),
            "manifest_path": "/nonexistent/manifest.md",
            "pid": None,
            "pid_start_time": None,
            "node": {
                "id": run_id.rsplit("-", 1)[-1],
                "plan": PLAN,
                "section": "§6",
                "time_budget": "40m",
                "write_paths": [],
            },
        },
    )


def _plant_scratch(run_id: str) -> Path:
    """A run's scratch directory holding a file, as a real worker would leave."""
    directory = dispatch.ensure_worker_scratch(run_id)
    (directory / "worker-temp.txt").write_text("scratch\n", encoding="utf-8")
    return directory


def _apply_negative_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: leave the scratch directory in place."""
    monkeypatch.setattr(
        promotion,
        "remove_worker_scratch",
        lambda _run_id, **_kwargs: {
            "scratch_removed": False,
            "scratch_path": None,
            "scratch_withheld": "scratch left in place by the negative control",
        },
    )


def test_a_dispatched_worker_tmpdir_is_its_scratch_directory(
    scratch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment the supervisor launches a worker with points TMPDIR at it."""
    run_id = "r-20260928T140000000000-scratch-dispatch"
    environment = dispatch._worker_runtime_environment(
        {},
        run_id=run_id,
        manifest_path="/nonexistent/manifest.md",
        attempt_started_at="2026-09-28T14:00:00Z",
        coordinator_session="s23-coord",
        claude_headers=False,
    )

    scratch = dispatch.worker_scratch_dir(run_id)
    assert environment["TMPDIR"] == str(scratch)
    assert Path(environment["TMPDIR"]).is_dir()
    assert scratch.name == run_id
    assert scratch.parent == dispatch.worker_scratch_root() == scratch_root

    # The process env the worker is actually spawned with keeps the run's
    # TMPDIR even though the operator's own TMPDIR is inherited alongside it.
    monkeypatch.setenv("TMPDIR", "/host/operator/tmp")
    launched = dispatch._worker_process_environment(environment, dialect="claude")
    assert launched["TMPDIR"] == str(scratch)


def test_promotion_removes_the_runs_scratch_directory(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed run's scratch is gone and the removal is reported."""
    if NEGATIVE_CONTROL:
        _apply_negative_control(monkeypatch)
    run_id = "r-20260928T140100000000-scratch-promote"
    worktree = _worktree(repository, tmp_path, "promotion")
    _pointer(run_id, repository=repository, worktree=worktree)
    scratch = _plant_scratch(run_id)
    assert scratch.is_dir()

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="scratch removal on promotion",
        completed_at="2026-09-28T14:05:00Z",
        root=repository,
    )

    release = promoted["release"]
    assert release["scratch_removed"] is True
    assert release["scratch_path"] == str(scratch)
    assert not scratch.exists(), "the run's scratch directory must be gone"
    assert f"removed worker scratch directory {scratch}" in capsys.readouterr().out


def test_discard_removes_the_runs_scratch_directory(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A discarded run's scratch is gone, a sibling run's survives, both reported."""
    if NEGATIVE_CONTROL:
        _apply_negative_control(monkeypatch)
    run_id = "r-20260928T140200000000-scratch-discard"
    other_id = "r-20260928T140250000000-scratch-sibling"
    worktree = _worktree(repository, tmp_path, "discard")
    _pointer(run_id, repository=repository, worktree=worktree, launch="cli")
    scratch = _plant_scratch(run_id)
    sibling = _plant_scratch(other_id)

    result = crew.discard(run_id)

    assert result["scratch_removed"] is True
    assert result["scratch_path"] == str(scratch)
    assert not scratch.exists(), "the discarded run's scratch must be gone"
    assert sibling.is_dir(), "a different run's scratch must survive"
    assert (sibling / "worker-temp.txt").is_file()
    assert f"removed worker scratch directory {scratch}" in capsys.readouterr().out


def test_the_real_scratch_root_is_untouched(
    scratch_root: Path, repository: Path, tmp_path: Path
) -> None:
    """Every path the removal resolves is the test's own scratch root."""
    run_id = "r-20260928T140300000000-scratch-isolated"
    worktree = _worktree(repository, tmp_path, "isolated")
    _pointer(run_id, repository=repository, worktree=worktree)
    scratch = _plant_scratch(run_id)

    crew.discard(run_id)

    assert dispatch.worker_scratch_root() == scratch_root
    assert scratch.parent == scratch_root
    assert not scratch_root.joinpath(run_id).exists()


def test_a_malformed_run_id_removes_nothing(scratch_root: Path, tmp_path: Path) -> None:
    """A run id that names no scratch directory is withheld, the root intact."""
    root = dispatch.worker_scratch_root()
    sibling = _plant_scratch("r-20260928T140500000000-scratch-survivor")
    marker = tmp_path / "outside.txt"
    marker.write_text("outside the scratch root\n", encoding="utf-8")

    # ".." resolves to the root's parent and "" to the root itself; both would
    # be removed by an rmtree that trusted Path(name).name == name.
    for bad in ("..", ".", "a/b", "/abs", ""):
        outcome = dispatch.remove_worker_scratch(bad)
        assert outcome["scratch_removed"] is False, bad
        assert outcome["scratch_withheld"], bad

    assert root.is_dir(), "the scratch root must survive"
    assert scratch_root.parent.is_dir(), "the root's parent must survive"
    assert sibling.is_dir(), "a well-formed sibling run's scratch must survive"
    assert marker.is_file(), "a file outside the root must survive"


def test_the_scratch_root_is_pinned_against_a_shared_tmpdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared-storage TMPDIR does not move the root; only the override does."""
    monkeypatch.delenv(dispatch.WORKER_SCRATCH_ROOT_ENV, raising=False)
    # The pinned root is the declared node-local default, not whatever TMPDIR
    # says: the root is asserted against the module's own constant rather than a
    # second literal, so it stays in step with the source that defines it.
    pinned = (
        Path(dispatch.WORKER_SCRATCH_ROOT_DEFAULT) / dispatch.WORKER_SCRATCH_ROOT_NAME
    )
    monkeypatch.setenv("TMPDIR", "/gpfs/scratch/shared-tmp")
    assert dispatch.worker_scratch_root() == pinned
    assert not str(dispatch.worker_scratch_root()).startswith("/gpfs")
    monkeypatch.delenv("TMPDIR", raising=False)
    assert dispatch.worker_scratch_root() == pinned


def test_promotion_removes_the_scratch_recorded_at_dispatch(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded directory is removed though the promoter's TMPDIR differs."""
    run_id = "r-20260928T140600000000-scratch-tmpdir"
    worktree = _worktree(repository, tmp_path, "tmpdir")
    _pointer(run_id, repository=repository, worktree=worktree)
    scratch = _plant_scratch(run_id)
    # The dispatcher ran under one TMPDIR and the promoter under another; the
    # root is pinned, and the path removed is the one dispatch recorded, so the
    # divergence cannot make the removal miss and leak the directory.
    monkeypatch.setenv("TMPDIR", "/gpfs/scratch/shared-tmp")

    promoted = crew.complete(
        run_id,
        gate="passed",
        outcome="scratch removal across a TMPDIR change",
        completed_at="2026-09-28T14:05:00Z",
        root=repository,
    )

    release = promoted["release"]
    assert release["scratch_removed"] is True
    assert release["scratch_path"] == str(scratch)
    assert not scratch.exists()
    assert f"removed worker scratch directory {scratch}" in capsys.readouterr().out


def _raise_audit(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("worktree audit refused")


def test_the_promotion_release_fallback_reports_the_scratch(
    scratch_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release step that raises still removes and reports the scratch."""
    run_id = "r-20260928T140650000000-scratch-promote-raises"
    scratch = _plant_scratch(run_id)
    record = {
        "run_id": run_id,
        "scratch": str(scratch),
        "worktree": str(tmp_path / "absent"),
        "repo": str(tmp_path),
        "pid": None,
    }
    monkeypatch.setattr(promotion, "_worktree_audit", _raise_audit)

    release = promotion._release_after_promotion(run_id, record, gate="passed")

    assert "scratch_removed" in release
    assert release["scratch_removed"] is True
    assert release["scratch_path"] == str(scratch)
    assert not scratch.exists()


def test_the_discard_release_fallback_reports_the_scratch(
    scratch_root: Path,
    repository: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release that raises on discard still reports the scratch outcome."""
    run_id = "r-20260928T140700000000-scratch-discard-raises"
    worktree = _worktree(repository, tmp_path, "discard-raises")
    _pointer(run_id, repository=repository, worktree=worktree, launch="cli")
    scratch = _plant_scratch(run_id)
    monkeypatch.setattr(promotion, "_worktree_audit", _raise_audit)

    result = crew.discard(run_id)

    assert result["scratch_removed"] is True
    assert result["scratch_path"] == str(scratch)
    assert not scratch.exists()
    assert f"removed worker scratch directory {scratch}" in capsys.readouterr().out


def _trust_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: remove whatever path the record names."""
    monkeypatch.setattr(
        dispatch_launch_module,
        "_removable_scratch_target",
        lambda run_id, recorded_path: (
            Path(recorded_path)
            if recorded_path
            else dispatch.worker_scratch_root() / str(run_id),
            "",
        ),
    )


def _accept_moved_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared mutation: drop the parent-of-the-current-root refusal.

    Every other check the removal makes is kept, so the only paths this admits
    that the real guard refuses are those whose parent is not the resolved
    scratch root — a record made under a root that has since moved. Applying it
    at the guard rather than in a caller shows the refusal is what withholds the
    directory, not a coincidental caller check.
    """

    def _accept(run_id: str, recorded_path: object) -> tuple[Path | None, str]:
        name = str(run_id or "").strip()
        if not name or name in {".", ".."} or Path(name).name != name:
            return None, "run id names no scratch directory"
        path = (
            Path(recorded_path)
            if recorded_path
            else dispatch.worker_scratch_root() / name
        )
        if path.is_symlink():
            return None, "scratch path is a symlink"
        if not path.is_dir():
            return None, "scratch directory is no longer present"
        return path, ""

    monkeypatch.setattr(dispatch_launch_module, "_removable_scratch_target", _accept)


def test_a_recorded_path_naming_a_sibling_is_withheld(
    scratch_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record naming a different run's directory removes nothing."""
    if RECORDED_CONTROL:
        _trust_recorded(monkeypatch)
    owner = "r-20260928T140800000000-scratch-owner"
    sibling = _plant_scratch("r-20260928T140810000000-scratch-sibling")

    outcome = dispatch.remove_worker_scratch(owner, recorded_path=str(sibling))

    assert outcome["scratch_removed"] is False
    assert outcome["scratch_withheld"]
    assert sibling.is_dir(), "the sibling run's scratch must survive"
    assert (sibling / "worker-temp.txt").is_file()


def test_a_recorded_path_outside_the_root_is_withheld(
    scratch_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record pointing outside the scratch root removes nothing."""
    if MOVED_ROOT_CONTROL:
        _accept_moved_root(monkeypatch)
    run_id = "r-20260928T140820000000-scratch-outside"
    outside = tmp_path / "away" / run_id
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")

    outcome = dispatch.remove_worker_scratch(run_id, recorded_path=str(outside))

    assert outcome["scratch_removed"] is False
    assert outcome["scratch_withheld"]
    assert outside.is_dir()
    assert (outside / "keep.txt").is_file()


def test_a_record_under_a_moved_root_is_withheld(
    scratch_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record made under a root that has since moved is left in place.

    The scratch root is redirected between the create and the remove, so the
    recorded path's parent is no longer the resolved root. That record must be
    withheld — removed False, a reason — and the directory it names must still
    be there afterwards.
    """
    if MOVED_ROOT_CONTROL:
        _accept_moved_root(monkeypatch)
    run_id = "r-20260928T140850000000-scratch-moved-root"
    created = _plant_scratch(run_id)
    recorded = str(created)
    assert created.is_dir()

    moved_root = tmp_path / "moved-scratch"
    monkeypatch.setenv(dispatch.WORKER_SCRATCH_ROOT_ENV, str(moved_root))
    assert dispatch.worker_scratch_root() == moved_root

    outcome = dispatch.remove_worker_scratch(run_id, recorded_path=recorded)

    assert outcome["scratch_removed"] is False
    assert outcome["scratch_withheld"]
    assert created.is_dir(), "a record under a moved root must be left in place"
    assert (created / "worker-temp.txt").is_file()


def test_a_recorded_symlink_is_withheld(scratch_root: Path, tmp_path: Path) -> None:
    """A recorded path that is a symlink removes nothing."""
    run_id = "r-20260928T140830000000-scratch-symlink"
    real = tmp_path / "real-scratch"
    real.mkdir(parents=True)
    (real / "keep.txt").write_text("keep\n", encoding="utf-8")
    scratch_root.mkdir(parents=True, exist_ok=True)
    link = dispatch.worker_scratch_dir(run_id)
    link.symlink_to(real, target_is_directory=True)

    outcome = dispatch.remove_worker_scratch(run_id, recorded_path=str(link))

    assert outcome["scratch_removed"] is False
    assert "symlink" in outcome["scratch_withheld"]
    assert link.is_symlink()
    assert real.is_dir()
    assert (real / "keep.txt").is_file()


def test_the_normal_recorded_path_is_removed(scratch_root: Path) -> None:
    """A record naming exactly the run's own directory is removed."""
    run_id = "r-20260928T140840000000-scratch-recorded-ok"
    scratch = _plant_scratch(run_id)

    outcome = dispatch.remove_worker_scratch(run_id, recorded_path=str(scratch))

    assert outcome["scratch_removed"] is True
    assert outcome["scratch_path"] == str(scratch)
    assert not scratch.exists()


# ── The negative control ────────────────────────────────────────────────────


def _observed_after(scratch_root: Path, tmp_path: Path) -> list[str]:
    """Report whether each removal leaves the scratch directory in place."""
    saved = os.environ.get(dispatch.WORKER_SCRATCH_ROOT_ENV)
    os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = str(scratch_root)
    lines: list[str] = []
    original = promotion.remove_worker_scratch
    # The declared mutation, applied directly: every removal becomes a no-op, so
    # the directory the measure expects gone is left in place.
    promotion.remove_worker_scratch = lambda _run_id, **_kwargs: {
        "scratch_removed": False,
        "scratch_path": None,
        "scratch_withheld": "scratch left in place by the negative control",
    }
    try:
        config_home = tmp_path / "config"
        config_home.mkdir(parents=True, exist_ok=True)
        os.environ["RECKON_HOME"] = str(config_home)
        run_id = "r-20260928T140400000000-scratch-negative"
        # A record whose worktree is absent: the release withholds the tree,
        # which is the path the scratch removal must not depend on. This is the
        # one release step both promotion and discard funnel through.
        record = {
            "run_id": run_id,
            "worktree": str(tmp_path / "absent"),
            "repo": str(tmp_path),
            "pid": None,
        }
        promote_scratch = _plant_scratch(run_id)
        promotion._release_run_workspace(record)
        lines.append(
            f"scratch present after promotion release: {promote_scratch.is_dir()}"
        )

        _pointer(run_id, repository=tmp_path, worktree=tmp_path / "absent")
        discard_scratch = _plant_scratch(run_id)
        try:
            crew.discard(run_id)
        except Exception as exc:  # noqa: BLE001 - the report carries the reason
            lines.append(f"discard raised: {exc}")
        lines.append(f"scratch present after discard: {discard_scratch.is_dir()}")
    finally:
        promotion.remove_worker_scratch = original
        if saved is None:
            os.environ.pop(dispatch.WORKER_SCRATCH_ROOT_ENV, None)
        else:
            os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = saved
    return lines


def _observed_recorded_trust(scratch_root: Path, tmp_path: Path) -> list[str]:
    """Report whether trusting a recorded path removes a sibling run's scratch."""
    saved = os.environ.get(dispatch.WORKER_SCRATCH_ROOT_ENV)
    os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = str(scratch_root)
    original = dispatch._removable_scratch_target
    # The declared mutation, applied directly: the recorded path is removed
    # without any check, so a record naming another run's directory deletes it.
    dispatch._removable_scratch_target = lambda run_id, recorded_path: (
        (Path(recorded_path) if recorded_path else scratch_root / str(run_id)),
        "",
    )
    lines: list[str] = []
    try:
        owner = "r-20260928T140900000000-scratch-owner"
        sibling = _plant_scratch("r-20260928T140910000000-scratch-sibling")
        dispatch.remove_worker_scratch(owner, recorded_path=str(sibling))
        lines.append(f"recorded sibling present after removal: {sibling.is_dir()}")
    finally:
        dispatch._removable_scratch_target = original
        if saved is None:
            os.environ.pop(dispatch.WORKER_SCRATCH_ROOT_ENV, None)
        else:
            os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = saved
    return lines


def _observed_moved_root(scratch_root: Path, tmp_path: Path) -> list[str]:
    """Report whether a record under a moved root removes its directory."""
    saved = os.environ.get(dispatch.WORKER_SCRATCH_ROOT_ENV)
    os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = str(scratch_root)
    original = dispatch._removable_scratch_target

    def _accept(run_id: str, recorded_path: object) -> tuple[Path | None, str]:
        # The declared mutation, applied directly: the parent-of-the-current-
        # root refusal is dropped, so a record under a moved root is accepted.
        name = str(run_id or "").strip()
        path = Path(recorded_path) if recorded_path else scratch_root / name
        if path.is_symlink() or not path.is_dir():
            return None, "scratch directory is no longer present"
        return path, ""

    dispatch._removable_scratch_target = _accept
    lines: list[str] = []
    try:
        run_id = "r-20260928T140950000000-scratch-moved-root"
        created = _plant_scratch(run_id)
        moved_root = tmp_path / "moved-scratch"
        os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = str(moved_root)
        dispatch.remove_worker_scratch(run_id, recorded_path=str(created))
        lines.append(f"recorded directory present after root moved: {created.is_dir()}")
    finally:
        dispatch._removable_scratch_target = original
        if saved is None:
            os.environ.pop(dispatch.WORKER_SCRATCH_ROOT_ENV, None)
        else:
            os.environ[dispatch.WORKER_SCRATCH_ROOT_ENV] = saved
    return lines


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    if MOVED_ROOT_CONTROL:
        print(MOVED_ROOT_CONTROL_MUTATION)
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for line in _observed_moved_root(temporary / "scratch", temporary):
                print(line)
    elif RECORDED_CONTROL:
        print(RECORDED_CONTROL_MUTATION)
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for line in _observed_recorded_trust(temporary / "scratch", temporary):
                print(line)
    else:
        print(NEGATIVE_CONTROL_MUTATION)
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for line in _observed_after(temporary / "scratch", temporary):
                print(line)
    sys.exit(0)
