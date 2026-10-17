"""Land a reviewed head as a merge commit without touching the shared index.

A coordinator lands a reviewed worker head on the primary branch in the shared
checkout, where promotions are writing their own commits at the same moment.
``git merge --no-ff`` writes the merged files into the working tree and stages
them before it commits, so when a concurrent writer wins the ``.git/index.lock``
race the merge leaves the checkout's index and working tree dirty and holds them
until someone repairs it by hand. Every follower and producer then defers its
reload while that tree is dirty.

This module builds the merge out of objects instead. ``git merge-tree
--write-tree`` computes the merged tree without reading or writing the index or
the working tree, and ``git commit-tree`` turns that tree into a commit. The
checkout is touched only by the final ``git merge --ff-only``, which either
completes in one step or leaves it exactly as it was, so a held lock or a moved
HEAD costs a retry rather than a repair.

Nothing here sets a plan, a sprint or a project index; it moves one branch and
pushes it. Identity is a module-local concern: an actor, if one arrives later,
belongs to the caller's scope and not to the landing.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

# The repository's own pre-push guard refuses a line-anchored ``co-authored-by``
# trailer, and a footer crediting a generator is the same defect in another
# spelling. Both are refused where the message is composed, so a message that
# would be rewritten by hand after the commit exists is rejected before a
# commit exists at all.
_ATTRIBUTION_TRAILER = re.compile(
    r"^\s*co-authored-by\s*:", re.IGNORECASE | re.MULTILINE
)
_GENERATOR_FOOTER = re.compile(
    r"generated with[^\n]*\b(?:claude|codex|copilot|gemini|gpt|an ai|ai)\b",
    re.IGNORECASE,
)

# A held index.lock is released by whichever process is committing, and HEAD may
# move in the same window, so a failed landing retries with a short backoff
# rather than losing the race permanently. The bound keeps a permanent lock a
# reported refusal rather than a hang.
DEFAULT_ATTEMPTS = 8
DEFAULT_RETRY_SECONDS = 0.25


class MergeError(Exception):
    """A landing refused with nothing written to the checkout's index or tree."""


def project_repository(project: str) -> Path:
    """Return the checkout a project's reviewed heads land in.

    The project's registered mount decides it, never the caller's enclosing
    checkout, so a coordinator landing from inside a worktree still moves the
    branch every follower and producer reads.
    """
    from reckon.crew.dispatch_sections import project_mount_repository

    mount = project_mount_repository(project)
    if mount is None:
        raise MergeError(
            f"project {project!r} has no registered mount to land a head in"
        )
    return mount


def _run(
    repo: Path, *args: str, input: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
    )


def _rev_parse(repo: Path, revision: str) -> str:
    result = _run(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    answer = result.stdout.strip()
    if result.returncode != 0 or not answer:
        raise MergeError(f"cannot resolve {revision!r} to a commit in {repo}")
    return answer


def _parents(repo: Path, commit: str) -> list[str]:
    result = _run(repo, "rev-parse", f"{commit}^@")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _compose_message(subject: str, body: str) -> str:
    """Return a two-part commit message, refusing an empty body or a credit.

    A bodiless subject fails the repository's review rule, and a trailer
    crediting a tool is refused for the same reason: the merge records the
    author's work, and the tooling that carried it is not a co-author. Both
    refusals happen before any object is written.
    """
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject:
        raise MergeError("a review head lands with a subject, not a bare body")
    if not body:
        raise MergeError(
            "a review head lands with a body; a subject-only merge message fails review"
        )
    for text in (subject, body):
        if _ATTRIBUTION_TRAILER.search(text) or _GENERATOR_FOOTER.search(text):
            raise MergeError(
                "refusing an attribution trailer in the merge message; the "
                "author's work is the authorship"
            )
    return f"{subject}\n\n{body}\n"


def _merge_tree(repo: Path, base: str, head: str) -> tuple[str, list[str]]:
    """Return the merged tree and any conflicted paths, touching no checkout state."""
    result = _run(repo, "merge-tree", "--write-tree", "--name-only", base, head)
    if result.returncode not in (0, 1):
        raise MergeError(
            "merge-tree failed: " + (result.stderr.strip() or result.stdout.strip())
        )
    lines = result.stdout.splitlines()
    if not lines:
        raise MergeError("merge-tree produced no tree")
    tree = lines[0].strip()
    conflicts = [line.strip() for line in lines[1:] if line.strip()]
    return tree, conflicts


def refusing_conflicts(conflicts: list[str]) -> list[str]:
    """Return the conflicted paths that stop a landing.

    A seam rather than a branch: a caller can present the merge closure over a
    head that conflicts, and the refusal is this one decision. Removing the
    refusal leaves the merge-tree result carrying conflict markers, which the
    final fast-forward then writes into the checkout.
    """
    return conflicts


def _commit_tree(repo: Path, tree: str, parents: list[str], message: str) -> str:
    args = ["commit-tree", tree]
    for parent in parents:
        args += ["-p", parent]
    args += ["-F", "-"]
    result = _run(repo, *args, input=message)
    commit = result.stdout.strip()
    if result.returncode != 0 or not commit:
        raise MergeError(
            "commit-tree failed: " + (result.stderr.strip() or "no output")
        )
    return commit


def build_merge_commit(
    repo: Path, base: str, head: str, subject: str, body: str
) -> str:
    """Build a merge commit of ``head`` onto ``base`` without touching the checkout."""
    message = _compose_message(subject, body)
    tree, conflicts = _merge_tree(repo, base, head)
    refusing = refusing_conflicts(conflicts)
    if refusing:
        raise MergeError(
            "the head conflicts; nothing was written: " + ", ".join(refusing)
        )
    return _commit_tree(repo, tree, [base, head], message)


def _git_dir(repo: Path) -> Path:
    result = _run(repo, "rev-parse", "--absolute-git-dir")
    return Path(result.stdout.strip() or (repo / ".git"))


def _index_lock_held(repo: Path) -> bool:
    return (_git_dir(repo) / "index.lock").exists()


def _current_branch(repo: Path) -> str:
    result = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise MergeError("HEAD is detached; check out the primary branch first")
    return branch


def _remote_head(repo: Path, remote: str, branch: str) -> str:
    result = _run(repo, "ls-remote", remote, f"refs/heads/{branch}")
    if result.returncode != 0 or not result.stdout.strip():
        raise MergeError(f"cannot read {remote}'s {branch}")
    return result.stdout.split()[0].strip()


def _push(repo: Path, remote: str, commit: str, branch: str) -> None:
    result = _run(repo, "push", remote, f"{commit}:refs/heads/{branch}")
    if result.returncode != 0:
        raise MergeError(
            f"push of {commit} to {remote} failed: "
            + (result.stderr.strip() or result.stdout.strip())
        )


def land_merge(
    repo: str | Path,
    *,
    head: str,
    message: str = "",
    body: str = "",
    candidate: str | None = None,
    remote: str = "origin",
    attempts: int = DEFAULT_ATTEMPTS,
    retry_seconds: float = DEFAULT_RETRY_SECONDS,
    sleep=time.sleep,
) -> dict:
    """Land ``head`` on the checked-out branch and push it, returning a report.

    The base is pinned to ``HEAD``, the merge is built with ``merge-tree`` and
    ``commit-tree``, and the branch moves with ``--ff-only``, which completes or
    leaves the checkout untouched. A held ``index.lock`` or a moved ``HEAD`` is
    retried after a short pause, and the commit is rebuilt from the fresh HEAD so
    the fast-forward stays a fast-forward. With ``candidate`` the already-built
    merge is landed unchanged, provided its parents are the pinned HEAD and the
    head.
    """
    repo = Path(repo)
    if candidate is None:
        _compose_message(message, body)
    head_sha = _rev_parse(repo, head)
    branch = _current_branch(repo)

    base = commit = ""
    for attempt in range(1, attempts + 1):
        base = _rev_parse(repo, "HEAD")
        if candidate is not None:
            commit = _rev_parse(repo, candidate)
            parents = _parents(repo, commit)
            if parents != [base, head_sha]:
                raise MergeError(
                    f"candidate {commit} has parents {parents}, not the pinned "
                    f"HEAD {base} and head {head_sha}"
                )
        else:
            commit = build_merge_commit(repo, base, head_sha, message, body)

        result = _run(repo, "merge", "--ff-only", commit)
        if result.returncode == 0:
            break
        if _index_lock_held(repo):
            if attempt == attempts:
                raise MergeError(
                    "the checkout's index.lock stayed held for "
                    f"{attempts} attempts; nothing was written"
                )
            sleep(retry_seconds)
            continue
        if _rev_parse(repo, "HEAD") != base:
            if attempt == attempts:
                raise MergeError("HEAD moved on each attempt; nothing was written")
            continue
        raise MergeError(
            "could not fast-forward: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    else:  # pragma: no cover - the loop raises or breaks
        raise MergeError(f"could not land after {attempts} attempts")

    landed = _rev_parse(repo, commit)
    _push(repo, remote, landed, branch)
    remote_head = _remote_head(repo, remote, branch)
    if remote_head != landed:
        raise MergeError(
            f"{remote}'s {branch} reads {remote_head}, not the landed {landed}"
        )
    return {
        "base": base,
        "head": head_sha,
        "commit": landed,
        "branch": branch,
        "remote": remote,
        "remote_head": remote_head,
    }
