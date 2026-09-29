#!/usr/bin/env python3
"""Measure whether six study commits fold without conflict once their record is a fragment.

Two arms are built in a scratch area outside this checkout, both from the base
the six commits share. Each arm re-applies the six commits' own file changes
onto that base, then folds the six with ``git merge-tree`` and counts the
conflicted paths under ``docs/plans`` and ``docs/evidence``.

``before``
    The six commits as they are. Each adds the one shared cumulative record, so
    the fold reports that path conflicted; the six plan edits touch disjoint
    regions and merge cleanly, which the log states as the measured count rather
    than as an expectation.

``after``
    Each commit's evidence anchor is moved into its own fragment under
    ``docs/evidence/fragments/<plan>/<node>.html`` and its plan edit is dropped
    (that edit becomes a ``landing:`` line recorded through the versioned write,
    not a git change). The fragments are keyed by node, so the fold reports no
    conflicted path, and ``reckon.evidence.compose_landed_record`` over the
    folded tree carries all six anchors.

Where the scratch lives, and why not a clone
--------------------------------------------
All git state the measure writes goes to a scratch object store under a private
temp directory, never to this repository's object store:

    GIT_OBJECT_DIRECTORY       = <scratch>/objects   (new objects land here)
    GIT_ALTERNATE_OBJECT_DIRECTORIES = <repo>/objects (existing objects read from here)
    GIT_INDEX_FILE             = <scratch>/index-*   (the repo index is never used)

The git commands run against this run's own worktree, because a crew worker's
git guard refuses a mutating verb that resolves to any other repository (a
clone and a sibling worktree are both refused). Redirecting the object store is
what keeps the repository untouched while the resolved repository stays this
run's own: the guard sees this worktree, and every byte the measure writes lands
in the scratch object store. The log checks that isolation rather than asserting
it — each arm's head commit must resolve under the scratch object store and must
not resolve against the repository's own store — because a bare object count
cannot say it (peers commit to that store concurrently).

Run it from the checkout whose ``reckon`` package should be composed; the log
names ``reckon.__file__`` so the run says which one it used.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path

BASE = "457bbec7e691465101ea7c56716fe23eb8477dfe"
# The six study commits, in the order the plan names them (velocity, worker
# anatomy, coordinator overhead, code depth, nova flapping, nova readiness).
COMMITS = [
    "9afcdbe0850a77635206a24ba83ee48907929c08",  # velocity
    "11fb09a4c059d3ddb19d6ff1a5fd4a4152847e7c",  # worker anatomy
    "4413e78a7c83012ab3e10eabc7fca311ed165d2f",  # coordinator overhead
    "77876fa0f96958e9553a208f76101e2c7b35520a",  # code depth
    "e01ec2ad23b7baef8fbcc4aafca83635872c3511",  # nova flapping
    "9cf52140f9fc3080b5e99d7447a41cc79867c7f5",  # nova readiness
]

PLAN = "orchestrator-crew-pattern-studies"
PROJECT = "reckon"
RECORD_REL = f"docs/evidence/archive/{PLAN}-landed.html"
PLAN_REL = f"docs/plans/{PLAN}.html"
CONFLICT_TREES = ("docs/plans/", "docs/evidence/")

# A conflicted path is reported by ``git merge-tree --write-tree`` as a stage
# entry ``<mode> <oid> <stage>\t<path>``. Informational lines (``Auto-merging``,
# ``CONFLICT``) do not match, so the entry form is what identifies a conflict
# rather than any line that happens to name a path.
_CONFLICT_ENTRY = re.compile(r"^\d{6} [0-9a-f]{40} [123]\t(.+)$")


class Arm:
    """The scratch object store and per-command environment one run writes to."""

    def __init__(self, repo: Path, scratch: Path) -> None:
        self.repo = repo
        self.scratch = scratch
        self.objects = scratch / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        common = self._rev("--git-common-dir")
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (repo / common_path).resolve()
        self.alternates = str(common_path / "objects")

    def _rev(self, *args: str) -> str:
        return git(self.repo, "rev-parse", *args).stdout.decode().strip()

    def env(self, index_name: str | None = None) -> dict[str, str]:
        env = {
            "GIT_OBJECT_DIRECTORY": str(self.objects),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": self.alternates,
        }
        if index_name is not None:
            env["GIT_INDEX_FILE"] = str(self.scratch / f"index-{index_name}")
        return env


def run(
    args: list[str],
    *,
    check: bool = True,
    input_bytes: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        args, capture_output=True, input=input_bytes, env=env, check=False
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed ({proc.returncode}): {' '.join(args)}\n"
            f"stdout: {proc.stdout.decode(errors='replace')}\n"
            f"stderr: {proc.stderr.decode(errors='replace')}"
        )
    return proc


def git(
    repo: Path, *args: str, env: dict[str, str] | None = None, **kwargs
) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args], extra_env=env, **kwargs)


def git_text(
    repo: Path, *args: str, env: dict[str, str] | None = None, **kwargs
) -> str:
    return git(repo, *args, env=env, **kwargs).stdout.decode(errors="replace")


def repo_toplevel() -> Path:
    """The checkout the script runs from — the git target and the composed module."""
    out = run(["git", "rev-parse", "--show-toplevel"]).stdout.decode().strip()
    return Path(out)


def resolvable(repo: Path, sha: str, env: dict[str, str] | None = None) -> bool:
    """Whether ``sha`` resolves in the repository, under ``env`` when given."""
    return git(repo, "cat-file", "-e", sha, env=env, check=False).returncode == 0


def present(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def changes_of(repo: Path, commit: str) -> list[tuple[str, str]]:
    out = git_text(repo, "diff", "--name-status", "-M", f"{commit}^", commit)
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        rows.append((status, path))
    return rows


def anchor_of(repo: Path, commit: str) -> tuple[str, str]:
    """Return the record anchor id the commit wrote and its section HTML.

    Each commit writes the shared record with its own section in it, so the
    record version that commit produced names its anchor exactly once.
    """
    text = git_text(repo, "show", f"{commit}:{RECORD_REL}")
    match = re.search(r'<section id="([^"]+)"', text)
    if not match:
        raise RuntimeError(f"{commit} carries no anchored section in {RECORD_REL}")
    start = text.rindex("<section", 0, match.end())
    end = text.index("</section>", match.end()) + len("</section>")
    return match.group(1), text[start:end]


def build_commit(
    arm: Arm,
    commit: str,
    changes: list[tuple[str, str]],
    *,
    after: bool,
    fragment_rel: str | None,
    fragment_bytes: bytes | None,
) -> str:
    """A commit whose parent is BASE and whose tree is the arm's rewrite."""
    env = arm.env(index_name=f"{commit[:12]}-{'after' if after else 'before'}")
    git(arm.repo, "read-tree", BASE, env=env)
    for _status, path in changes:
        if after and path in (RECORD_REL, PLAN_REL):
            continue
        blob = git_text(arm.repo, "rev-parse", f"{commit}:{path}").strip()
        git(
            arm.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{path}",
            env=env,
        )
    if after:
        assert fragment_rel is not None and fragment_bytes is not None
        blob = (
            run(
                ["git", "-C", str(arm.repo), "hash-object", "-w", "--stdin"],
                input_bytes=fragment_bytes,
                extra_env=env,
            )
            .stdout.decode()
            .strip()
        )
        git(
            arm.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},{fragment_rel}",
            env=env,
        )
    tree = git_text(arm.repo, "write-tree", env=env).strip()
    subject = git_text(arm.repo, "log", "-1", "--format=%s", commit).strip()
    arm_name = "after" if after else "before"
    return git_text(
        arm.repo,
        "commit-tree",
        tree,
        "-p",
        BASE,
        "-m",
        f"{subject} [{arm_name} arm: rewritten onto base]",
        env=env,
    ).strip()


def fold(arm: Arm, commits: list[str]) -> tuple[str, list[str]]:
    """Fold commits sequentially with merge-tree; return the head and conflicts.

    ``git merge-tree --write-tree`` merges two commits, so the six are folded one
    at a time: each fold's result tree is committed with the accumulation as its
    parent, and the next commit is merged onto that. The merge base stays the
    arm's base throughout, so each step applies one commit's changes onto the
    accumulation exactly as a pairwise merge would.
    """
    env = arm.env(index_name="fold")
    acc = BASE
    conflicted: list[str] = []
    for commit in commits:
        proc = git(
            arm.repo, "merge-tree", "--write-tree", acc, commit, env=env, check=False
        )
        lines = proc.stdout.decode(errors="replace").splitlines()
        if not lines:
            raise RuntimeError(f"merge-tree produced no output for {commit}")
        tree = lines[0].strip()
        for line in lines[1:]:
            match = _CONFLICT_ENTRY.match(line)
            if match and match.group(1) not in conflicted:
                conflicted.append(match.group(1))
        acc = git_text(
            arm.repo, "commit-tree", tree, "-p", acc, "-m", "fold", env=env
        ).strip()
    return acc, conflicted


def materialize(arm: Arm, tree: str, destination: Path) -> None:
    tar_bytes = git(arm.repo, "archive", "--format=tar", tree, env=arm.env()).stdout
    with tarfile.open(fileobj=BytesIO(tar_bytes)) as archive:
        archive.extractall(destination, filter="data")


def record_stub() -> bytes:
    """The cumulative record's own bytes, with no anchor of its own."""
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        f'  <meta name="docs-project" content="{PROJECT}">\n'
        '  <meta name="reckon-type" content="evidence">\n'
        f'  <meta name="plan-evidence-for" content="{PLAN}">\n'
        f"  <title>{PLAN} — landed record | {PROJECT}</title>\n"
        "</head>\n"
        "<body>\n"
        '  <main class="plan-doc">\n'
        f"    <h1>{PLAN} — landed record</h1>\n"
        "  </main>\n"
        "</body>\n"
        "</html>\n"
    ).encode()


def compose(arm: Arm, tree: str, scratch: Path) -> tuple[bytes, str]:
    """Materialise the folded tree, write the record stub, compose, return bytes."""
    root = scratch / "after-extract"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir()
    materialize(arm, tree, root)
    record_path = root / RECORD_REL
    record_path.write_bytes(record_stub())

    source = repo_toplevel()
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    import reckon
    import reckon.evidence as evidence_module

    composed = evidence_module.compose_landed_record(
        record_path, PLAN, project=PROJECT, root=root
    )
    return composed, str(Path(reckon.__file__))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-scratch",
        action="store_true",
        help="leave the scratch object store in place for inspection",
    )
    args = parser.parse_args()

    repo = repo_toplevel()
    common = Path(
        run(["git", "-C", str(repo), "rev-parse", "--git-common-dir"])
        .stdout.decode()
        .strip()
    )
    if not common.is_absolute():
        common = (repo / common).resolve()

    print(f"MODULE_SOURCE_CHECKOUT: {repo}")
    print(f"CWD: {Path.cwd().resolve()}")
    print(f"REVISION: {git_text(repo, 'rev-parse', 'HEAD').strip()}")
    print(f"REPOSITORY: {common}")

    scratch = Path(tempfile.mkdtemp(prefix="landing-record-merge-measure-"))
    print(f"SCRATCH: {scratch}")
    arm = Arm(repo, scratch)

    exit_code = 1
    try:
        present_commits = [c for c in COMMITS if present(repo, c)]
        missing = [c for c in COMMITS if c not in present_commits]
        print(f"BASE: {BASE}")
        if missing:
            print(f"MISSING_COMMITS: {' '.join(missing)}")
        else:
            print("MISSING_COMMITS: none")

        plans: list[tuple[str, list[tuple[str, str]], str, str]] = []
        for commit in present_commits:
            changes = changes_of(repo, commit)
            anchor_id, section = anchor_of(repo, commit)
            plans.append((commit, changes, anchor_id, section))
        anchors = [anchor for _c, _ch, anchor, _s in plans]

        before_commits = [
            build_commit(
                arm,
                commit,
                changes,
                after=False,
                fragment_rel=None,
                fragment_bytes=None,
            )
            for commit, changes, _a, _s in plans
        ]
        after_commits = [
            build_commit(
                arm,
                commit,
                changes,
                after=True,
                fragment_rel=f"docs/evidence/fragments/{PLAN}/{anchor}.html",
                fragment_bytes=section.encode(),
            )
            for commit, changes, anchor, section in plans
        ]

        before_head, before_conflicts = fold(arm, before_commits)
        after_head, after_conflicts = fold(arm, after_commits)

        def relevant(paths: list[str]) -> list[str]:
            return [p for p in paths if p.startswith(CONFLICT_TREES)]

        before_relevant = relevant(before_conflicts)
        after_relevant = relevant(after_conflicts)
        print(
            "BEFORE: conflicted_paths="
            f"{len(before_relevant)} {' '.join(before_relevant) or '(none)'}"
        )
        print(
            "AFTER: conflicted_paths="
            f"{len(after_relevant)} {' '.join(after_relevant) or '(none)'}"
        )
        if before_conflicts != before_relevant:
            print(
                f"BEFORE_OTHER_TREES: {' '.join(p for p in before_conflicts if p not in before_relevant)}"
            )
        if after_conflicts != after_relevant:
            print(
                f"AFTER_OTHER_TREES: {' '.join(p for p in after_conflicts if p not in after_relevant)}"
            )

        composed, module_file = compose(arm, after_head, scratch)

        # Control: the same stub with no fragments must carry no anchor, so the
        # anchor check is shown to see something rather than to always pass.
        control = arm.scratch / "control"
        control.mkdir()
        (control / RECORD_REL).parent.mkdir(parents=True, exist_ok=True)
        (control / RECORD_REL).write_bytes(record_stub())
        import reckon.evidence as evidence_module

        bare = evidence_module.compose_landed_record(
            control / RECORD_REL, PLAN, project=PROJECT, root=control
        )
        control_counts = {a: bare.count(f'id="{a}"'.encode()) for a in anchors}

        counts = {
            anchor: composed.count(f'id="{anchor}"'.encode()) for anchor in anchors
        }
        print(f"MODULE_FILE: {module_file}")
        print(f"COMPOSED_BYTES: {len(composed)}")
        for anchor in anchors:
            print(f"ANCHOR {anchor}: found {counts[anchor]} time(s)")
        print(
            "CONTROL_NO_FRAGMENTS: anchors_found="
            f"{sum(control_counts.values())} (expected 0)"
        )

        anchors_ok = all(counts[anchor] == 1 for anchor in anchors)
        control_ok = sum(control_counts.values()) == 0

        # The scratch isolation check: each arm's head commit is one object the
        # measure produced. It must resolve under the scratch object store and
        # must NOT resolve against the repository's own store, so the arm wrote
        # nothing to the shared object directory. A bare object count cannot say
        # this — peers commit to that store concurrently — so the check names
        # the objects the measure itself made.
        arm_heads = (before_head, after_head)
        leaked = [sha for sha in arm_heads if resolvable(repo, sha)]
        isolated = (
            all(resolvable(repo, sha, arm.env()) for sha in arm_heads) and not leaked
        )
        print(f"SCRATCH_OBJECTS_ISOLATED: {isolated}")
        if leaked:
            print(f"LEAKED_TO_SHARED_STORE: {' '.join(leaked)}")

        ok = (
            not missing
            and len(before_relevant) > 0
            and not after_relevant
            and anchors_ok
            and control_ok
            and isolated
        )
        print(f"ANCHORS_OK: {anchors_ok}")
        print(f"CONTROL_OK: {control_ok}")
        print(f"EXIT: {'ok' if ok else 'not-ok'}")
        exit_code = 0 if ok else 1
    finally:
        if args.keep_scratch:
            print(f"SCRATCH_RETAINED: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
