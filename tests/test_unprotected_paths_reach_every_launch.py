"""A removed protected default reaches every launch and the durable record.

The protected set is a flight key whose built-in list is the shipped default and
whose only way to drop one is to name it under ``unprotected_paths``. Primary
dispatch threads the resolved flight config into fence composition and records
the removed defaults on the run's pointer. Two other launch paths compose a
fence too — an in-place resume and a lane-change redispatch — and both build
their argv through the same plan builder, so a config that never reaches them
re-seals a default the layer left writable. The ledger row is the durable half:
a run that left a default writable must carry that list onto its committed row
once promotion deletes the live pointer.

Three properties, each on the launch path it names:

* a resumed run's composed fence leaves the removed default writable and keeps
  the ones the layer did not name, and the resumed run carries the removed list
  on its pointer;
* a lane-changed run's composed fence does the same, and the lane-changed run
  carries the removed list on its pointer;
* promoting a run whose pointer carries the removed list writes it onto the
  ledger row beside ``fence_waiver``.

The declared negative control drops the fence config from the resume launch
path in a scratch tree; the resumed run then re-seals the default and case 1
fails. Running this file directly prints that mutation verbatim on its first
line and the observed composition beneath it.
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from reckon import crew, ledger
from reckon.crew.runs import pointer_path
from tests import test_a_live_run_never_reads_dead as liveness

dispatch_module = importlib.import_module("reckon.crew.dispatch")

PROJECT = "proj"

# The default this node's layer leaves out, and one it leaves sealed.
REMOVED = ".codex"
KEPT = ".claude"

# The declared mutation, printed verbatim as the red log's first line.
NEGATIVE_CONTROL_MUTATION = (
    "drop the fence_config argument from the resume launch path in a scratch "
    "tree; the resumed run then re-seals the unprotected default and case 1 fails"
)

CONFIG = {
    "default_backend": "alpha",
    "backends": {
        "alpha": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
        "beta": {
            "launch": "cli",
            "command": "codex",
            "sandbox": "worktree-full",
            "time_budget": "25m",
            "session_reuse": True,
            "usable_input_window": 512_000,
        },
        "native": {"launch": "in-harness", "time_budget": "25m"},
    },
    "roles": {"implement": {}, "inline": {"backend": "native"}},
    "budget": {
        "utilisation_ceiling_pct": 100,
        "resume_reserve_pct": 5,
        "exhausted_statuses": [],
    },
    "fences": {"time_budget": "25m", "needs_help_after_failures": 2},
    "unprotected_paths": [REMOVED],
}


@pytest.fixture()
def operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary operator home exposing the shipped default protected set.

    ``HOME`` is patched so the composed fence, the built-in default and the
    layer's ``unprotected_paths`` entry all resolve against this tree and name
    nothing in the operator's real home.
    """
    home = tmp_path / "operator-home"
    for name in (
        ".claude",
        ".codex",
        ".ssh",
        ".agents",
        ".config/reckon",
        ".config/git",
        "public",
        ".local/bin",
    ):
        (home / name).mkdir(parents=True)
    # The defaults that are files rather than directories are created as files:
    # a ``.gitconfig`` directory makes every ``git`` in this home refuse.
    for name in (".claude.json", ".gitconfig"):
        (home / name).write_text("", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def crew_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the crew config and state home at a temp tree."""
    config_home = tmp_path / "config"
    config_home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(config_home))
    return config_home


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, crew_home: Path) -> Path:
    """A throwaway repository carrying a plan and the fleet launch script."""
    root = tmp_path / "repo"
    (root / "docs" / "plans").mkdir(parents=True)
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    scripts = root / "skills" / "reckon-build" / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).parents[1] / "skills/reckon-build/scripts/worktree_fleet.py"
    (scripts / "worktree_fleet.py").write_text(source.read_text(encoding="utf-8"))
    (root / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
""",
        encoding="utf-8",
    )
    (root / "allowed.txt").write_text("seed\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "allowed.txt", "docs/plans/plan-a.html", "skills"),
        ("commit", "-q", "-m", "chore: seed"),
    ):
        _git(root, *args)
    (crew_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _node(crew_home: Path) -> crew.TaskNode:
    manifest = crew_home / "node-manifests" / "node-a-manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    return crew.TaskNode(
        id="node-a",
        goal="carry the removed protected default through every launch",
        plan="plan-a",
        section="§3",
        done_when="tests/test_unprotected_paths_reach_every_launch.py passes",
        write_paths=["allowed.txt"],
        time_budget="25m",
        manifest_path=str(manifest),
        spec_level="guided",
        role="implement",
    )


def _stopped_pointer(tmp_path: Path, repo: Path, run_id: str, *, backend: str) -> dict:
    """A cli run as a resume or a lane change finds it: gone, on this host.

    The supervisor's exit record is the observation that licenses the second
    worker, so the launch under test is reached rather than refused.
    """
    directory = crew.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tree = tmp_path / f"{run_id}-tree"
    tree.mkdir(parents=True, exist_ok=True)
    manifest = directory / "manifest.md"
    manifest.write_text(f"node: {run_id}\nstatus: waiting\n", encoding="utf-8")
    prompt_path = directory / "prompt.txt"
    prompt_path.write_text("the original dispatch prompt\n", encoding="utf-8")
    liveness._write_exit_record(run_id)
    record = {
        "run_id": run_id,
        "project": PROJECT,
        "repo": str(repo),
        "worktree": str(tree),
        "launch": "cli",
        "backend": backend,
        "dialect": "codex",
        "role": "implement",
        "pid": liveness._absent_pid(),
        "pid_start_time": None,
        "process_alive": None,
        "session_id": f"sess-{run_id}",
        "session_harness": "codex",
        "attempt": 1,
        "base_sha": "HEAD",
        "created_at": "2026-09-29T09:59:00+00:00",
        "log_path": str(directory / "stream.jsonl"),
        "manifest_path": str(manifest),
        "prompt_path": str(prompt_path),
        "phase": "working",
        "node": {
            "id": "node-a",
            "plan": "plan-a",
            "section": "§3",
            "time_budget": "25m",
            "write_paths": ["allowed.txt"],
        },
    }
    crew._write_json(pointer_path(run_id), record)
    return record


def _sealed(argv, target: str) -> bool:
    """Whether ``argv`` mounts ``target`` read-only."""
    for index, word in enumerate(argv):
        if word == "--ro-bind" and list(argv[index + 1 : index + 3]) == [
            target,
            target,
        ]:
            return True
    return False


def test_case_1_a_resumed_run_leaves_the_default_writable(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resume under a removing layer leaves the named default writable."""
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    run_id = "r-resume-unprotected"
    _stopped_pointer(tmp_path, repo, run_id, backend="alpha")

    plan = crew.resume_plan(run_id, "continue", config=CONFIG)

    argv = [str(token) for token in plan.argv]
    removed = str(operator_home / REMOVED)
    kept = str(operator_home / KEPT)
    assert not _sealed(argv, removed), (
        "the resumed fence re-sealed the default its layer left writable"
    )
    assert _sealed(argv, kept), "the resumed fence dropped a default nobody named"
    pointer = crew.read_pointer(run_id)
    assert removed in pointer["fence_unprotected_paths"]


def test_case_2_a_lane_changed_run_leaves_the_default_writable(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane change to a cli backend leaves the named default writable."""
    monkeypatch.setattr(dispatch_module, "FENCE_WORKERS", True)
    run_id = "r-lane-unprotected"
    _stopped_pointer(tmp_path, repo, run_id, backend="alpha")

    # The destination resolution is stubbed to a cli backend so the fence is
    # composed for real; the budget and ledger gates the move runs through are
    # stubbed to the smallest shape they are read as. Case 2 turns only on the
    # fence the lane change composes, which is the plan builder's, not the
    # stub's.
    resolution = SimpleNamespace(
        backend="beta",
        launch="cli",
        backend_settings=dict(CONFIG["backends"]["beta"]),
        lane_gate={
            "state": "open",
            "gate_path": "fixture-gate.json",
            "paused": False,
            "reason": None,
            "detail": "",
            "path_check": "skipped",
            "path_check_detail": "backend declares no lane document to publish a config_path",
        },
        validation=SimpleNamespace(ok=True, findings=[]),
        competence={"allowed": True},
        authority="a-ledger-authority",
        sandbox_write_roots=None,
    )
    monkeypatch.setattr(dispatch_module, "plan_dispatch", lambda **kwargs: resolution)
    monkeypatch.setattr(
        dispatch_module, "_budget_verdict", lambda **kwargs: {"held": False}
    )
    monkeypatch.setattr(
        dispatch_module, "resolve_dispatch_ledger_root", lambda authority: authority
    )

    moved = dispatch_module.change_lane(
        run_id,
        "beta",
        "the lane is spent",
        config=CONFIG,
        launch=True,
        launcher=lambda *args, **kwargs: 4242,
    )

    argv = [str(token) for token in moved["argv"]]
    removed = str(operator_home / REMOVED)
    assert not _sealed(argv, removed), (
        "the lane-changed fence re-sealed the default its layer left writable"
    )
    pointer = crew.read_pointer(run_id)
    assert removed in pointer["fence_unprotected_paths"]


def test_case_3_promotion_writes_the_list_onto_the_ledger_row(
    operator_home: Path,
    crew_home: Path,
    repo: Path,
) -> None:
    """A run that left a default writable carries the list onto its committed row."""
    record = dispatch_module.dispatch(
        node=_node(crew_home),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="unprotected-session",
        check_budget=False,
        launcher=lambda *args, **kwargs: 4242,
    )
    run_id = str(record["run_id"])
    worktree = Path(str(record["worktree"]))
    (worktree / "allowed.txt").write_text("seed\nwork\n", encoding="utf-8")
    _git(worktree, "add", "allowed.txt")
    _git(worktree, "commit", "-q", "-m", "test: worker edit")
    commit = _git(worktree, "rev-parse", "HEAD")

    crew.complete(run_id, gate="passed", commits=[commit], root=repo)

    rows = ledger.runs(PROJECT, root=repo)
    row = next(item for item in rows if item.get("run_id") == run_id)
    assert any(
        str(path).endswith(REMOVED) for path in row["fence_unprotected_paths"]
    ), row.get("fence_unprotected_paths")


def _negative_control_report(root: Path) -> list[str]:
    """Reproduce the red log: the resume path built without its fence config."""
    import os

    home = root / "operator-home"
    for name in (REMOVED, KEPT):
        (home / name).mkdir(parents=True, exist_ok=True)
    config_home = root / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    tree = root / "repo"
    tree.mkdir(parents=True, exist_ok=True)
    run_id = "r-negative-control"

    saved_home = os.environ.get("HOME")
    saved_reckon = os.environ.get("RECKON_HOME")
    os.environ["HOME"] = str(home)
    os.environ["RECKON_HOME"] = str(config_home)
    # The mutation the node declares: the resume launch composed without its
    # fence config, exactly as it read before this change.
    original = dispatch_module._backends.launch_plan

    def stripped(**kwargs):
        kwargs.pop("fence_config", None)
        return original(**kwargs)

    try:
        _stopped_pointer(root, tree, run_id, backend="alpha")
        dispatch_module._backends.launch_plan = stripped
        plan = crew.resume_plan(run_id, "continue", config=CONFIG)
    finally:
        dispatch_module._backends.launch_plan = original
        pointer_field = crew.read_pointer(run_id).get("fence_unprotected_paths")
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_reckon is None:
            os.environ.pop("RECKON_HOME", None)
        else:
            os.environ["RECKON_HOME"] = saved_reckon
    argv = [str(token) for token in plan.argv]
    removed = str(home / REMOVED)
    return [
        f"removed default     : {removed}",
        f"argc                : {len(argv)}",
        f"default still sealed: {_sealed(argv, removed)}",
        f"pointer field       : {pointer_field!r}",
        f"resume argv         : {json.dumps(argv)}",
    ]


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
