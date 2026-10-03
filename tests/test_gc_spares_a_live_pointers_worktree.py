"""A sweep spares every worktree a live pointer names, and acts on one run on request.

A run parked between turns keeps its live pointer while its manifest reads
waiting, its head still equals its dispatch base, and its worker process is not
alive. The phase-gated claim read that pointer as finished, so a repository-wide
sweep classified a live run's tree as reclaimable and removed it. The claim is
now the pointer's own: any worktree a live pointer names is live-referenced,
whatever phase, process liveness or integration state the pointer carries, and
only promotion or discard — which remove the pointer — release it. A held
tree's remedy is a sweep confined to that run, so clearing one duty cannot
reach a peer's tree.

Every case works on synthesised pointers, ledgers and repositories under a
temporary config home; no real fleet directory is read or written.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from reckon import cli, ledger
from reckon.crew.runs import _write_json, pointer_path, run_dir

routing_module = importlib.import_module("reckon.crew.routing")
obligations_module = importlib.import_module("reckon.crew.obligations")

PROJECT = "live-pointer-gc-fixture"
SESSION = "gc-fixture-session"
RUN_PARKED = "r-20261003T120048839648-parked-node"
RUN_PHASELESS = "r-20261003T120048839649-phaseless-node"
RUN_RETAINED = "r-20261003T120048839650-retained-node"
RUN_HELD = "r-20261003T120048839651-held-node"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised checkout whose project keeps its ledger under docs/state."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "docs" / "state" / PROJECT).mkdir(parents=True)
    (repository / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed the gc fixture"),
    ):
        _git(repository, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(repository / "docs")}), encoding="utf-8"
    )
    return repository


def _worktree(repository: Path, name: str) -> Path:
    """A managed tree at the same head as the repository, clean and integrated."""
    root = routing_module._workspace_roots(repository)[0]
    tree = root / SESSION / name
    tree.parent.mkdir(parents=True, exist_ok=True)
    _git(repository, "worktree", "add", "-q", "--detach", str(tree), "HEAD")
    return tree


def _dead_pid() -> int:
    """A pid the kernel reports as gone: a reaped child."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def _park(
    repository: Path, run_id: str, tree: Path, *, phase: str | None = "complete"
) -> None:
    """A live pointer for a run parked mid-measure, and its waiting manifest."""
    pointer: dict[str, object] = {
        "run_id": run_id,
        "pid": _dead_pid(),
        "worktree": str(tree),
        "repo": str(repository),
        "project": PROJECT,
        "node": tree.name,
    }
    if phase is not None:
        pointer["phase"] = phase
    _write_json(Path(pointer_path(run_id)), pointer)
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.md").write_text(
        "node: parked-node\n"
        "status: waiting\n"
        "wait_condition: job 1280609 finishes the forward solve\n"
        'wait_probe: ["squeue", "-h", "-j", "1280609"]\n'
        "wait_terminal: exit:0\n"
        "resume_brief: read the job log and continue\n",
        encoding="utf-8",
    )


def _retained(repository: Path, run_id: str, tree: Path) -> None:
    """A promoted run's ledger row, whose retained tree is still registered."""
    retained_at = datetime.now(tz=UTC).isoformat()
    record = ledger.build_record(
        run_id=run_id,
        plan="fixture-plan",
        gate="passed",
        node=tree.name,
        completed_at=retained_at,
    )
    record["worktree_retention"] = {
        "classification": "retained-for-resume",
        "worktree": str(tree.resolve()),
        "session_id": "fixture-session",
        "session_source": "pointer",
        "retained_at": retained_at,
    }
    ledger.append_run(PROJECT, record, root=repository, allow_create=True)


def test_a_sweep_spares_a_parked_runs_worktree(fleet: Path) -> None:
    """A parked run's tree survives an applying sweep, whichever phase it carries.

    The two parked runs cover both arms of the phase-gated predicate: a
    terminal-looking phase, and no phase at all with a dead worker.
    """
    phased = _worktree(fleet, "parked-node")
    _park(fleet, RUN_PARKED, phased)
    phaseless = _worktree(fleet, "phaseless-node")
    _park(fleet, RUN_PHASELESS, phaseless, phase=None)

    report = routing_module.garbage_collect(repo=fleet, project=PROJECT, apply=True)

    rows = {item["path"]: item for item in report["worktrees"]}
    assert rows[str(phased)]["classification"] == "live-referenced"
    assert rows[str(phased)]["claimed_by_live_runs"] == [RUN_PARKED]
    assert rows[str(phaseless)]["classification"] == "live-referenced"
    assert rows[str(phaseless)]["claimed_by_live_runs"] == [RUN_PHASELESS]
    assert phased.is_dir()
    assert phaseless.is_dir()
    assert report["removed_worktrees"] == []


def test_run_scope_touches_only_the_named_runs_tree(fleet: Path) -> None:
    """``gc --run`` removes the named run's tree and never lists a peer's."""
    named = _worktree(fleet, "retained-node")
    _retained(fleet, RUN_RETAINED, named)
    parked = _worktree(fleet, "parked-node")
    _park(fleet, RUN_PARKED, parked)

    result = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(fleet),
            "--project",
            PROJECT,
            "--run",
            RUN_RETAINED,
            "--apply",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [item["path"] for item in payload["worktrees"]] == [str(named)]
    assert payload["removed_worktrees"] == [str(named)]
    assert not named.is_dir()
    assert parked.is_dir()

    scoped_to_live = CliRunner().invoke(
        cli.main,
        [
            "crew",
            "gc",
            "--repo",
            str(fleet),
            "--project",
            PROJECT,
            "--run",
            RUN_PARKED,
            "--apply",
        ],
    )

    assert scoped_to_live.exit_code == 0, scoped_to_live.output
    live_payload = json.loads(scoped_to_live.output)
    assert [item["path"] for item in live_payload["worktrees"]] == [str(parked)]
    assert live_payload["worktrees"][0]["classification"] == "live-referenced"
    assert live_payload["removed_worktrees"] == []
    assert parked.is_dir()


def test_the_held_tree_remedy_names_a_run_scoped_sweep(fleet: Path) -> None:
    """A held tree's remedy confines the sweep to that run, and holds no released tree."""
    tree = _worktree(fleet, "held-node")
    _retained(fleet, RUN_HELD, tree)
    now = datetime.now(tz=UTC)

    held = obligations_module._held_worktrees(PROJECT, SESSION, now=now)

    assert [item["run_id"] for item in held] == [RUN_HELD]
    assert held[0]["kind"] == "worktree-held"
    assert held[0]["next_command"] == (
        f"reckon crew gc --repo {fleet.resolve()} --project {PROJECT} "
        f"--run {RUN_HELD} --apply"
    )

    # A registration whose directory is gone holds nothing: the reader checks
    # the tree at read time rather than showing a row from a stale snapshot.
    shutil.rmtree(tree)
    registered = [
        line.removeprefix("worktree ")
        for line in _git(fleet, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]
    assert str(tree.resolve()) in registered
    assert not tree.is_dir()
    assert obligations_module._held_worktrees(PROJECT, SESSION, now=now) == []
