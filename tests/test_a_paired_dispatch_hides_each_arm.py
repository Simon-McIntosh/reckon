"""A paired dispatch starts both arms from one base, each fenced from the other.

A shadow dispatched after its primary has landed can read the answer from the
project's main checkout or from git history, where the fence does not reach. So
a paired dispatch starts the two arms together: both run ids, both run
directories and both worktree paths are fixed before either arm launches, and
each arm's fence is composed from its partner's *allocation* rather than from a
live pointer or a ledger row the partner does not yet have.

The mask is asserted by *executing* the argument vector ``launch_plan``
composes, never by inspecting its shape: a shape assertion cannot tell a mask
that holds from one spelled correctly that does nothing. A stub stands in for
the harness — it lists its partner's run directory and writes a probe into its
own run directory and worktree — and the test reads the record back.

The declared negative control drops the primary arm's masks, and its
empty-listing assertion must then fail. Running this file directly reproduces
that red log: its first line is the declared mutation, verbatim, and what
follows is the observed non-empty listing.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from reckon import _backends
from reckon.crew import promotion_scope
from reckon.crew.runs import new_run_id, run_dir
from tests import test_crew as _test_crew

# ``reckon.crew`` is a facade module that re-exports the dispatcher's ``dispatch``
# function under that name, so the dispatcher's own module is reached by import
# rather than by the facade attribute, which resolves to the function.
dispatch_module = importlib.import_module("reckon.crew.dispatch")

CONFIG = _test_crew.CONFIG
_node = _test_crew._node
home = _test_crew.home
repo = _test_crew.repo

PAIR_BACKEND = "candidate"
PROJECT = "proj"

# The environment variable the declared mutation is applied through: with it set
# the primary arm composes no masks, so its empty-listing assertion fails.
DROP_PRIMARY_MASKS_ENV = "RECKON_TEST_DROP_PAIR_PRIMARY_MASKS"

NEGATIVE_CONTROL_MUTATION = (
    "the primary arm's masks dropped, so its empty-listing assertion must fail"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

# Stands in for the harness: lists the partner's run directory (masked to an
# empty tmpfs, so any entry is the leak under test) and proves the arm's own
# own run directory and worktree stay writable.
_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
printf 'partner-run-dir [' >> "$FENCE_RESULTS"
ls -A "$FENCE_PARTNER_RUN" 2>&1 | tr '\\n' ',' >> "$FENCE_RESULTS"
printf ']\\n' >> "$FENCE_RESULTS"
if touch "$FENCE_OWN_RUN/.pair-write-probe" 2>/dev/null; then
  echo "own-run-write-ok" >> "$FENCE_RESULTS"
else
  echo "own-run-write-refused" >> "$FENCE_RESULTS"
fi
if touch "$FENCE_OWN_WORKTREE/.pair-write-probe" 2>/dev/null; then
  echo "own-worktree-write-ok" >> "$FENCE_RESULTS"
else
  echo "own-worktree-write-refused" >> "$FENCE_RESULTS"
fi
"""


def _candidate_config() -> dict:
    """Route implement nodes to a distinct configured candidate backend."""
    import json

    config = json.loads(json.dumps(CONFIG))
    config["backends"]["candidate"] = {
        **config["backends"]["alpha"],
        "model": "candidate-model",
    }
    config["roles"]["implement"] = {"backend": "candidate"}
    return config


class Arm:
    """One allocated arm: its run directory, worktree path and masks."""

    def __init__(
        self,
        root: Path,
        name: str,
        *,
        own_id: str,
        partner_id: str,
        partner_name: str,
    ) -> None:
        self.root = root
        self.home = root / "home"
        self.reckon_home = self.home / ".config" / "reckon"
        self.reckon_home.mkdir(parents=True, exist_ok=True)
        os.environ["RECKON_HOME"] = str(self.reckon_home)
        os.environ["HOME"] = str(self.home)
        self.run_id = own_id
        self.partner_run_id = partner_id
        self.run = run_dir(self.run_id)
        self.run.mkdir(parents=True, exist_ok=True)
        self.partner_run = run_dir(self.partner_run_id)
        self.partner_run.mkdir(parents=True, exist_ok=True)
        self.worktree = root / f"{name}-worktree"
        self.worktree.mkdir(exist_ok=True)
        self.partner_worktree = root / f"{partner_name}-worktree"
        self.partner_worktree.mkdir(exist_ok=True)
        self.stub = root / "stub.sh"
        self.stub.write_text(_STUB)
        self.stub.chmod(0o755)
        self.results = self.run / "results.txt"

    def masks(self, *, drop: bool = False) -> tuple[str, ...]:
        if drop:
            return ()
        return promotion_scope._allocated_read_surfaces(
            run_id=self.partner_run_id,
            worktree=str(self.partner_worktree),
            project=PROJECT,
        )

    def argv(self, *, drop: bool = False) -> list[str]:
        return _backends.launch_plan(
            backend_name="b",
            backend={
                "launch": "cli",
                "command": str(self.stub),
                "dialect": "claude",
                "sandbox": "worktree-full",
            },
            prompt="p",
            worktree=str(self.worktree),
            manifest_path=str(self.run / "manifest.md"),
            writable_directories=[str(self.run), str(self.worktree)],
            fence=True,
            fence_home=str(self.home),
            masked_paths=self.masks(drop=drop),
        ).argv

    def run_fence(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "HOME": str(self.home),
            "FENCE_RESULTS": str(self.results),
            "FENCE_PARTNER_RUN": str(self.partner_run),
            "FENCE_OWN_RUN": str(self.run),
            "FENCE_OWN_WORKTREE": str(self.worktree),
        }
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.worktree),
            capture_output=True,
            text=True,
            check=False,
        )


def test_a_paired_dry_run_reports_two_arms_at_one_base(home, repo) -> None:
    """One call resolves a primary arm and a shadow arm at one base revision.

    Both run ids and both run directories are allocated before either arm
    launches, and the shadow arm's lineage names the primary while the primary's
    resolution names the shadow as its pair.
    """
    result = dispatch_module.dispatch_pair(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=_candidate_config(),
        session="sess",
        pair_backend=PAIR_BACKEND,
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["paired"] is not None
    assert result["primary_run_id"] != result["shadow_run_id"]
    assert result["base_sha"]
    # Both run directories were created before either arm launched.
    assert run_dir(result["primary_run_id"]).is_dir()
    assert run_dir(result["shadow_run_id"]).is_dir()
    assert result["shadow"]["lineage"]["kind"] == "shadow"
    assert result["shadow"]["lineage"]["primary_run_id"] == result["primary_run_id"]
    assert result["primary"]["paired_shadow_run_id"] == result["shadow_run_id"]
    # Both arms name a worktree path, and neither is the other's.
    assert result["primary"]["worktree"] != result["shadow"]["worktree"]
    assert result["primary"]["worktree"] and result["shadow"]["worktree"]


def test_the_pair_masks_name_the_partners_run_directory(home, repo) -> None:
    """Each arm's masks name the partner's allocated run directory.

    The masks are resolved through ``promotion_scope._allocated_read_surfaces``
    from the allocation, so they hold before the partner has a pointer or a
    ledger row.
    """
    result = dispatch_module.dispatch_pair(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=_candidate_config(),
        session="sess",
        pair_backend=PAIR_BACKEND,
        dry_run=True,
    )
    assert str(run_dir(result["shadow_run_id"])) in result["primary_masks"]
    assert str(run_dir(result["primary_run_id"])) in result["shadow_masks"]
    # An arm's own run directory is never its own mask: the fence masks the
    # partner, not the records the arm is about to write.
    assert str(run_dir(result["primary_run_id"])) not in result["primary_masks"]
    assert str(run_dir(result["shadow_run_id"])) not in result["shadow_masks"]
    # A surface that could not be resolved contributes no mask.
    assert (
        promotion_scope._allocated_read_surfaces(
            run_id="", worktree="", project=PROJECT
        )
        == ()
    )


def _paired_arms(root: Path) -> tuple[Arm, Arm]:
    """Two arms over one pair of allocated run ids, each naming the other."""
    run_a = new_run_id("primary-node")
    run_b = new_run_id("shadow-node")
    primary = Arm(
        root, "primary-node", own_id=run_a, partner_id=run_b, partner_name="shadow-node"
    )
    shadow = Arm(
        root, "shadow-node", own_id=run_b, partner_id=run_a, partner_name="primary-node"
    )
    return primary, shadow


@requires_bwrap
def test_neither_arm_can_read_its_partners_run_directory(
    tmp_path: Path,
) -> None:
    """Both arms list the partner's run directory as empty and can still write."""
    primary, shadow = _paired_arms(tmp_path)
    # Real content on both sides, so a mask that did not hold would list it.
    (primary.run / "stream.jsonl").write_text('{"secret": "primary"}\n')
    (shadow.run / "stream.jsonl").write_text('{"secret": "shadow"}\n')

    drop = os.environ.get(DROP_PRIMARY_MASKS_ENV) == "1"
    for arm, drop_this in ((primary, drop), (shadow, False)):
        if not drop_this:
            # The same listing with the mask withheld shows the leak the mask
            # prevents; otherwise the empty listing proves nothing.
            assert "stream.jsonl" in "\n".join(
                str(path) for path in primary.partner_run.iterdir()
            )
        proc = arm.run_fence(arm.argv(drop=drop_this))
        assert proc.returncode == 0, proc.stderr
        results = arm.results.read_text()
        assert results.strip(), "the stub wrote nothing; the fence did not run it"
        assert "partner-run-dir []" in results, results
        assert "own-run-write-ok" in results, results
        assert "own-worktree-write-ok" in results, results
        assert (arm.run / ".pair-write-probe").exists()
        assert (arm.worktree / ".pair-write-probe").exists()


def test_an_unpaired_dispatch_composes_no_masks(home, repo) -> None:
    """A dispatch that is not paired passes no partner surfaces to the launch."""
    record = dispatch_module.dispatch(
        node=_node(spec_level="guided"),
        project=PROJECT,
        repo=repo,
        config=CONFIG,
        session="sess",
        launcher=lambda plan, *, log_path, stderr_path, prompt_path: 0,
    )
    assert "paired_shadow_run_id" not in record
    plan = _backends.launch_plan(
        backend_name="b",
        backend={"launch": "cli", "command": "true", "dialect": "claude"},
        prompt="p",
        worktree=record["worktree"],
        manifest_path=record["manifest_path"],
        fence=True,
        fence_home=str(home),
    )
    assert "--tmpfs" not in plan.argv


def _negative_control_report() -> int:
    """Run the muted arm directly and print its red log; 1 when it did not fail."""
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="pair-negative-control-"))
    print(NEGATIVE_CONTROL_MUTATION)
    os.environ[DROP_PRIMARY_MASKS_ENV] = "1"
    primary, shadow = _paired_arms(root)
    (primary.run / "stream.jsonl").write_text('{"secret": "primary"}\n')
    (shadow.run / "stream.jsonl").write_text('{"secret": "shadow"}\n')
    proc = primary.run_fence(primary.argv(drop=True))
    results = (primary.run / "results.txt").read_text()
    print(results)
    if "partner-run-dir []" in results:
        print("EXIT=0")
        return 0
    print(f"stub exit {proc.returncode}")
    print("EXIT=1")
    return 1


if __name__ == "__main__":
    sys.exit(_negative_control_report())
