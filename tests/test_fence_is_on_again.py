"""The worker default is fenced, proved through the dispatch path.

The fence is the worker default again. Every reason it was switched off is
closed on main: each run owns a harness home carrying the operator's hooks and
instruction files, a fenced worker can commit in its own worktree, a plan write
from inside a run stays in that run's worktree, and the fence refuses a
protected checkout rather than granting it. This file pins the default now that
it is on:

* ``dispatch.FENCE_WORKERS`` is true, so a launch composed through the dispatch
  path is fenced — the plan a launcher receives starts with the fence binary;
* a clive launch composed through the dispatch path names the run's own harness
  home in ``CLAUDE_CONFIG_DIR``, and that home carries the operator home's Stop
  hook and instruction file;
* the operator home itself is overlaid read-only, so the fence's seal rides the
  same composed argv.

The declared mutation leaves ``FENCE_WORKERS`` false and re-runs the same
dispatch-path case; the composed argv is then unfenced, which is what the check
above must fail against.

Nothing here needs a live fence binary: the composition is asserted on the argv,
and the fence's behaviour under a real worker is proved by running it in
``tests/test_a_fenced_worker_can_commit.py``.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from reckon import _backends, crew
from tests.test_crew import CONFIG, _node

pytest_plugins = ("tests.test_crew",)

# ``reckon.crew.dispatch`` names the command function on the package, so the
# module is reached by import rather than by attribute.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

# The declared mutation, printed verbatim as the red log's first line.
NEGATIVE_CONTROL_MUTATION = (
    "leave FENCE_WORKERS False; the dispatch-path fenced case must fail "
    "with an unfenced argv"
)

OPERATOR_HOOKS = {
    "Stop": [{"hooks": [{"type": "command", "command": "operator-stop-hook"}]}],
    "PreToolUse": [{"hooks": [{"type": "command", "command": "operator-guard"}]}],
}
OPERATOR_GUIDANCE = "# operator guidance\n\nNever restore paths you did not write.\n"

CLIVE_CONFIG = copy.deepcopy(CONFIG)
CLIVE_CONFIG["default_backend"] = "clive"
CLIVE_CONFIG["backends"]["clive"] = {
    "launch": "cli",
    "command": "clive",
    "model": "open-weight",
    "effort": "high",
    "sandbox": "worktree-full",
    "session_reuse": True,
    "time_budget": "25m",
}


def _operator_home(root: Path) -> Path:
    """A synthetic operator home whose harness reads hooks and guidance."""
    home = root / "operator-home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": OPERATOR_HOOKS,
                "env": {"OPERATOR_SECRET": "must-not-be-copied"},
                "permissions": {"allow": ["Bash"]},
            }
        )
    )
    (home / ".claude" / "CLAUDE.md").write_text(OPERATOR_GUIDANCE)
    (home / ".agents").mkdir()
    (home / "Code").mkdir()
    return home


def _dispatch_through_the_path(
    repo: Path, *, config: dict
) -> tuple[dict, _backends.LaunchPlan]:
    """Compose one launch through ``crew.dispatch`` with a capturing launcher."""
    launched: dict[str, object] = {}

    def launcher(plan, *, log_path, stderr_path, prompt_path):
        launched["plan"] = plan
        log_path.write_text("")
        return 4242

    record = crew.dispatch(
        node=_node(manifest_path=""),
        project="proj",
        repo=repo,
        config=config,
        session="sess",
        launcher=launcher,
    )
    return record, launched["plan"]


def _bind_pairs(argv: list[str]) -> list[tuple[str, str, str]]:
    """Every ``--bind``/``--ro-bind`` triple, in argv order."""
    return [
        (argv[i], argv[i + 1], argv[i + 1])
        for i in range(len(argv) - 1)
        if argv[i] in ("--bind", "--ro-bind")
    ]


def test_the_worker_default_is_fenced() -> None:
    """The switch a real dispatch reads now composes the fence."""
    assert dispatch_module.FENCE_WORKERS is True


def test_a_dispatch_path_clive_launch_is_fenced_and_carries_the_operator_hooks(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch path composes a fence and the run's own harness home.

    The harness home is read from the composed plan's own environment rather
    than recomputed, so the file the assertions open is the one the launch
    names. The operator home is a synthetic one patched in for ``Path.home``,
    so nothing here reads the real home the fleet writes to.
    """
    operator_home = _operator_home(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: operator_home))

    record, plan = _dispatch_through_the_path(repo=repo, config=CLIVE_CONFIG)

    argv = [str(token) for token in plan.argv]
    # The launch resolves the fence through PATH, so argv[0] is the fence's
    # absolute path; the property is that the fence is the composed command.
    assert Path(argv[0]).name == _backends.FENCE_BINARY, argv[:4]
    # The fence seals the operator's harness home read-only, so the run's own
    # home is what the harness actually reads.
    sealed_claude = str((operator_home / ".claude").resolve())
    assert ("--ro-bind", sealed_claude, sealed_claude) in _bind_pairs(argv)

    harness = Path(plan.environment["CLAUDE_CONFIG_DIR"])
    run_directory = Path(str(record["manifest_path"])).parent
    assert harness == run_directory / "harness"
    assert harness.is_dir()
    seeded = json.loads((harness / "settings.json").read_text())
    assert seeded["hooks"] == OPERATOR_HOOKS
    # Only the hooks key crossed: a credential or an allow-list in the operator
    # settings must not be copied into the run home.
    assert set(seeded) == {"hooks"}
    assert (harness / "CLAUDE.md").read_text() == OPERATOR_GUIDANCE


def _negative_control_report(root: Path) -> list[str]:
    """Run the control without pytest's monkeypatch, restoring by hand.

    The declared mutation is applied by hand: ``FENCE_WORKERS`` is left false,
    so the dispatch path composes an unfenced argv. The report names what the
    fenced case asserts, to show the mutation is what makes it fail.
    """
    operator_home = _operator_home(root)
    config_home = root / "config"
    config_home.mkdir(parents=True, exist_ok=True)
    repo = _synthetic_repo(root, config_home)
    saved_home = os.environ.get("RECKON_HOME")
    saved_path_home = Path.home
    saved_fence = dispatch_module.FENCE_WORKERS
    os.environ["RECKON_HOME"] = str(config_home)
    Path.home = classmethod(lambda cls: operator_home)
    dispatch_module.FENCE_WORKERS = False
    try:
        applied = dispatch_module.FENCE_WORKERS
        _record, plan = _dispatch_through_the_path(repo=repo, config=CLIVE_CONFIG)
    finally:
        dispatch_module.FENCE_WORKERS = saved_fence
        Path.home = saved_path_home
        if saved_home is None:
            os.environ.pop("RECKON_HOME", None)
        else:
            os.environ["RECKON_HOME"] = saved_home
    argv = [str(token) for token in plan.argv]
    fenced = bool(argv) and Path(argv[0]).name == _backends.FENCE_BINARY
    try:
        assert fenced, argv[:4]
        outcome = "the dispatch-path fenced case PASSED, so the control did nothing"
    except AssertionError:
        outcome = "the dispatch-path fenced case FAILED as declared"
    return [
        f"FENCE_WORKERS           : {applied}",
        f"argv[0]                 : {argv[0] if argv else ''}",
        f"fence binary is argv[0] : {fenced}",
        f"CLAUDE_CONFIG_DIR       : {plan.environment.get('CLAUDE_CONFIG_DIR', '')}",
        f"outcome                 : {outcome}",
    ]


def _synthetic_repo(root: Path, config_home: Path) -> Path:
    """A throwaway git repository carrying the worktree fleet script and plan."""
    repo = root / "repo"
    (repo / "skills" / "reckon-build" / "scripts").mkdir(parents=True)
    (repo / "docs" / "plans").mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "skills"
        / "reckon-build"
        / "scripts"
        / "worktree_fleet.py"
    )
    (repo / "skills" / "reckon-build" / "scripts" / "worktree_fleet.py").write_text(
        source.read_text()
    )
    (repo / "docs" / "plans" / "plan-a.html").write_text(
        """<!doctype html>
<html><head>
<meta name="docs-project" content="proj">
<meta name="reckon-type" content="plan">
<meta name="plan-slug" content="plan-a">
</head><body><h2 id="s3">§3 — Dispatch</h2></body></html>
"""
    )
    (repo / "seed.txt").write_text("seed\n")
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "worker@example.invalid"],
        ["config", "user.name", "Worker"],
        ["add", "seed.txt", "skills", "docs/plans/plan-a.html"],
        ["commit", "-q", "-m", "chore: seed"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (config_home / "mounts.json").write_text(json.dumps({"proj": str(repo / "docs")}))
    return repo


if __name__ == "__main__":  # pragma: no cover - reproduces the red log
    print(NEGATIVE_CONTROL_MUTATION)
    with tempfile.TemporaryDirectory() as directory:
        for line in _negative_control_report(Path(directory)):
            print(line)
    sys.exit(0)
