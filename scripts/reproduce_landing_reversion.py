#!/usr/bin/env python3
"""Reproduce, in a throwaway repository, the state a promotion left behind.

On 2026-10-18 a promotion of review run
``r-20261018T171142922335-review-of-census-proposal-coverage-f7ed7e9d8ef6``
committed its landing (``2321fba57``) into nova's shared checkout and then left
that checkout holding a *reversed* copy of the same commit: HEAD carried the
landing, while the index and the working tree held the pre-landing content at
the tracked plan file and were staged as deletions at the three paths the
commit created.

This script builds a four-path stand-in for that checkout and runs two arms:

* **interleaving** — a landing commit taken through a private index (the case
  where the committing process used another index file) racing a
  rollback that resolves HEAD to the commit *before* the landing. It ends with
  HEAD carrying the landing and the shared index and working tree staged back
  to the pre-landing content, the created paths absent from disk.
* **control** — the same landing with no rollback. It ends with all four paths
  equal in HEAD, the index and the working tree.

The rollback arm calls reckon's own rollback, imported from
``reckon.crew.promotion_scope._restore_landing_writes`` (line 1672) and invoked
directly, so the script exercises the production function rather than a copy of
it. That function issues, per path:

  ``git restore --source=HEAD --staged --worktree -- <path>``   (lines 1697-1706)
  ``git rm --cached --force -- <path>`` then unlink the file    (lines 1737, 1741)

When it runs while HEAD still resolves to the commit before the landing,
``--source=HEAD`` is the pre-landing commit; run after HEAD carries the landing
the same calls change nothing, because the landing's own paths are then in HEAD.
The private index in the interleaving arm lets the rollback's shared-index calls
succeed while the landing commit is still in flight, which is what a committing
process using another index file permits.

Usage:  PYTHONPATH=<worktree> python3 scripts/reproduce_landing_reversion.py
Exit status is 0 only when both arms produce their expected end state; the log
prints the resolved path of the production function so the reader can see which
module was exercised.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from reckon.crew.promotion_scope import _restore_landing_writes

PLAN = "docs/plans/converged-forward-solve.html"
CREATED = [
    "docs/state/nova/reviews/run/r-20261018T145203666018-census-proposal-coverage/legacy-5ef79bad605b.json",
    "docs/state/nova/reviews/run/r-20261018T145203666018-census-proposal-coverage/r-20261018T171142922335-review-of-census-proposal-coverage-f7ed7e9d8ef6.json",
    "docs/state/nova/runs/r-20261018T171142922335-review-of-census-proposal-coverage-f7ed7e9d8ef6.json",
]
PATHS = [PLAN, *CREATED]
PRE_PLAN = "plan\nmeta plan-version 395\nend\n"
LANDING_PLAN = "plan\nmeta plan-version 396\n# promote(...) landed comment\nend\n"


def git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=full_env,
        check=False,
    )


def must(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    r = git(repo, *args, env=env)
    if r.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}"
        )
    return r.stdout


def write_landing_writes(repo: Path) -> None:
    (repo / PLAN).write_text(LANDING_PLAN)
    for rel in CREATED:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"review": "landed"}\n')


def reckon_rollback(repo: Path) -> dict[str, bool]:
    """Run reckon's own rollback against the repository's current HEAD.

    Calls the production ``_restore_landing_writes`` imported from
    ``reckon.crew.promotion_scope`` (line 1672), with absolute paths, exactly as
    ``_commit_landing_writes`` (lines 1953, 1980) and the review-record refusal
    (``reckon/crew/promotion.py:1334``) call it.
    """
    return _restore_landing_writes(repo, [resolve(repo, rel) for rel in PATHS])


def resolve(repo: Path, rel: str) -> Path:
    return (repo / rel).resolve()


def fresh_repo(root: Path) -> tuple[Path, str]:
    repo = root / "checkout"
    repo.mkdir(parents=True)
    must(repo, "init", "-q", ".")
    must(repo, "config", "user.email", "repro@local")
    must(repo, "config", "user.name", "repro")
    (repo / PLAN).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN).write_text(PRE_PLAN)
    must(repo, "add", "--", PLAN)
    must(repo, "commit", "-qm", "before the landing")
    return repo, must(repo, "rev-parse", "HEAD").strip()


def snapshot(repo: Path, state: dict[str, str], label: str) -> None:
    head = must(repo, "rev-parse", "HEAD").strip()
    for rel in PATHS:
        in_head = git(repo, "cat-file", "-e", f"HEAD:{rel}").returncode == 0
        staged = (
            git(repo, "diff", "--cached", "--name-only", "--", rel).stdout.strip() != ""
        )
        on_disk = (repo / rel).exists()
        if not in_head:
            state[f"{label}:{rel}"] = "absent-in-HEAD"
        elif staged:
            state[f"{label}:{rel}"] = (
                "in-HEAD, reverted" if on_disk else "in-HEAD, staged-deleted"
            )
        else:
            state[f"{label}:{rel}"] = "equal"
    state[f"{label}:head-moved"] = "yes" if head != state.get(f"{label}:base") else "no"


def run_arm(root: Path, interleaving: bool) -> tuple[dict[str, str], str]:
    repo, pre = fresh_repo(root)
    state: dict[str, str] = {}
    write_landing_writes(repo)

    if interleaving:
        # The committing process stages into a private index (GIT_INDEX_FILE), so
        # the shared index is free while HEAD is still the pre-landing commit.
        priv = root / "private.index"
        env = {"GIT_INDEX_FILE": str(priv)}
        must(repo, "read-tree", "HEAD", env=env)  # private index = pre-landing tree
        must(repo, "add", "--", *PATHS, env=env)  # private index = landing content
        # The rollback runs now, while --source=HEAD is still the pre-landing commit.
        report = reckon_rollback(repo)
        print("  reckon rollback report:")
        for absolute, reverted in report.items():
            print(f"    {reverted!s:<5} {Path(absolute).relative_to(repo)}")
        # The landing commit lands from the private index, carrying the landing.
        must(repo, "commit", "-qm", "promote(...): not-run", env=env)
    else:
        must(repo, "add", "--", *PATHS)
        must(repo, "commit", "-qm", "promote(...): not-run")

    state["base"] = pre
    snapshot(repo, state, "arm")
    head = must(repo, "rev-parse", "HEAD").strip()
    return state, head


def report(title: str, state: dict[str, str]) -> None:
    print(f"\n=== {title} ===")
    for rel in PATHS:
        print(f"  {state['arm:' + rel]:<28} {rel}")
    print(f"  HEAD moved: {state['arm:head-moved']}")


def production_rollback_module() -> str:
    """Name the production rollback module and which copy of it was imported."""
    path = Path(sys.modules[_restore_landing_writes.__module__].__file__).resolve()
    try:
        shown = path.relative_to(Path(__file__).resolve().parents[1]).as_posix()
    except ValueError:
        shown = str(path)
    return f"{_restore_landing_writes.__module__} ({shown})"


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="landing-reversion-"))
    try:
        ok = True
        print(f"production rollback: {production_rollback_module()}")

        replay, _ = run_arm(root / "interleaving", interleaving=True)
        report(
            "interleaving: landing committed, rollback resolved pre-landing HEAD",
            replay,
        )
        want_reverted = {"equal", "in-HEAD, reverted", "in-HEAD, staged-deleted"}
        for rel in PATHS:
            if replay["arm:" + rel] == "equal":
                ok = False
                print(f"  UNEXPECTED: {rel} was left equal")
        if (
            all(replay["arm:" + r] in want_reverted for r in PATHS)
            and replay["arm:" + PLAN] == "in-HEAD, reverted"
            and all(replay["arm:" + r] == "in-HEAD, staged-deleted" for r in CREATED)
            and replay["arm:head-moved"] == "yes"
        ):
            print("  -> reproduces the reverted state: HEAD carries the landing, the")
            print("     plan path is staged back to pre-landing content, and the three")
            print("     created paths are absent from disk and staged as deletions.")
        else:
            ok = False
            print("  -> did NOT reproduce the reverted state")

        control, _ = run_arm(root / "control", interleaving=False)
        report("control: same landing, no rollback", control)
        if (
            all(control["arm:" + r] == "equal" for r in PATHS)
            and control["arm:head-moved"] == "yes"
        ):
            print(
                "  -> negative control holds: all four paths equal in HEAD, index and worktree."
            )
        else:
            ok = False
            print("  -> negative control FAILED: paths not left equal")

        print(f"\nworkspace: {root}")
        return 0 if ok else 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
