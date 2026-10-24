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

The push is the one step that can fail after the branch has moved, and a
landing that hides that reads as a refusal that wrote nothing. A push that
fails after the fast-forward raises :class:`MergePushOutstandingError`, which
carries the report naming the landed commit and the base; a caller reruns the
same head, finds it already in ``HEAD`` and pushes without building a second
merge. A host that refuses SSH on port 22 — every compute node — retries the
push over the port-443 route, the caller's ``GIT_SSH_COMMAND`` or the stated
fallback beside it.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable
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

# The port-443 route: GitHub serves SSH there for hosts that refuse port 22.
# ``GIT_SSH_COMMAND`` carries it because git builds the ssh invocation itself,
# so naming ``ssh.github.com:443`` there routes the push without a wrapper.
FALLBACK_SSH_COMMAND = "ssh -o Hostname=ssh.github.com -o Port=443"

# A landed merge whose push failed exits with a status of its own, so a caller
# distinguishes it from a refusal that wrote nothing (exit 1) and does not
# rebuild or double-handle a merge already in place.
PUSH_OUTSTANDING_EXIT = 6


class MergeError(Exception):
    """A landing refused with nothing written to the checkout's index or tree."""


class MergePushOutstandingError(MergeError):
    """The merge landed and only the push is outstanding.

    The branch has moved, so the outcome is not a refusal: ``report`` names the
    landed commit and the base, and the caller reruns the same head to retry the
    push rather than landing a second merge.
    """

    def __init__(self, report: dict, detail: str) -> None:
        self.report = report
        super().__init__(
            "the merge landed; only the push is outstanding (rerun to push): " + detail
        )


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
    repo: Path,
    *args: str,
    input: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _rev_parse(repo: Path, revision: str) -> str:
    """Resolve ``revision`` to its commit through the one shared resolver.

    The landing refuses a revision that names no commit, and it asks the same
    bounded lookup every other citation reader uses, so a spelling that resolves
    for one reader resolves for all of them. A refusal here is the caller's
    ``MergeError`` rather than the resolver's empty answer.
    """
    from reckon.crew.recovery_review_subject import _resolve_commit

    answer = _resolve_commit(repo, revision)
    if not answer:
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


# A marker is a line the change introduced or removed, taken from the head's
# diff against the base. A merge resolved to drop the reviewed change — the
# base side taken throughout — leaves the primary branch without the lines the
# change added, and the landing is refused there, before the branch moves. Lines
# shorter than this occur everywhere in a file, so their presence would not show
# that a change had survived; a marker has to be distinctive to be evidence.
_MARKER_MIN_LENGTH = 8


def _show_bytes(repo: Path, revision: str, path: str) -> bytes | None:
    """Return the bytes of ``path`` at ``revision``, or None when it is absent.

    The content is returned undecoded, because a changed path may be a binary
    blob and decoding it as text is exactly what a landing must not do before it
    takes a reading. A content read is the one git call that bypasses
    :func:`_run`; every other call stays a text run.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def _decode_text(data: bytes | None) -> str | None:
    """Return ``data`` as text, or None when it is not text.

    Content is text when it holds no NUL byte — git's own marker for binary —
    and decodes as UTF-8. Anything else is a blob whose lines carry no meaning
    the marker comparison could read, so it is compared by identity instead.
    """
    if data is None or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _blob_id(repo: Path, revision: str, path: str) -> str | None:
    """Return ``path``'s blob id at ``revision``, or None when it is absent.

    ``git rev-parse <revision>:<path>`` answers from the object database and
    prints the object's name, so it resolves a binary path as readily as a text
    content — ``rev-parse`` names an object rather than reading its bytes — and
    resolves an absent path to nothing.
    """
    result = _run(repo, "rev-parse", "--verify", "--quiet", f"{revision}:{path}")
    answer = result.stdout.strip()
    if result.returncode != 0 or not answer:
        return None
    return answer


def _read_path(
    repo: Path, revision: str, path: str, *, matches: str | None = None
) -> dict[str, object]:
    """Read ``path`` at ``revision`` and say whether it is the change's own.

    This is the one place the landing decides how a changed path is read, so
    :func:`change_markers` and :func:`surviving_change_refusals` call it rather
    than each reaching for a git call of its own. A reading carries:

    * ``present`` — whether ``path`` exists at ``revision``;
    * ``blob`` — its object id there, which names a binary blob as readily as a
      text one and decodes neither;
    * ``identical`` — whether ``blob`` equals ``matches``, the head's blob id;
    * ``text`` — the content decoded when it is text, and None when it is
      binary or was not read.

    ``matches`` carries the head's blob id, and it is what makes "survived" mean
    the same thing for every kind of change:

    * a path whose blob equals the head's survived, whether it is text or
      binary, and its content is not read at all — identical blobs carry the
      change by construction, so a binary path costs a decode it never needs;
    * a path whose blob differs was altered by the merge, so its content is read
      and returned as ``text`` (None when it is binary), which is where the
      marker comparison runs for a text path and where an altered binary path
      is refused;
    * with ``matches`` None, as when the head's markers are first computed, the
      content is read to become the reading's ``text``.
    """
    blob = _blob_id(repo, revision, path)
    reading: dict[str, object] = {
        "present": blob is not None,
        "blob": blob,
        "identical": False,
        "text": None,
    }
    if blob is None:
        return reading
    if matches is not None:
        reading["identical"] = blob == matches
        if reading["identical"]:
            return reading
    reading["text"] = _decode_text(_show_bytes(repo, revision, path))
    return reading


def _marker_lines(text: str) -> list[str]:
    """Return the distinctive lines of ``text``, in order and without repeats."""
    markers: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if len(stripped) >= _MARKER_MIN_LENGTH and stripped not in markers:
            markers.append(stripped)
    return markers


def _merge_base(repo: Path, left: str, right: str) -> str:
    """Return the common ancestor of ``left`` and ``right``, or ``left`` itself.

    An unrelated pair has no common ancestor, and a head with no shared history
    stands as its own base rather than raising: the change is then the whole of
    it, which is the strongest reading of what a landing must keep.
    """
    result = _run(repo, "merge-base", left, right)
    answer = result.stdout.strip()
    return answer if result.returncode == 0 and answer else left


def _changed_paths(repo: Path, base: str, head: str) -> list[tuple[str, str]]:
    """Return (status, path) for every path the head changed against the base."""
    result = _run(repo, "diff", "--name-status", "--no-renames", "-z", base, head)
    if result.returncode != 0:
        raise MergeError(
            "cannot read the head's change: "
            + (result.stderr.strip() or result.stdout.strip())
        )
    fields = [field for field in result.stdout.split("\0") if field]
    return [
        (fields[index], fields[index + 1]) for index in range(0, len(fields) - 1, 2)
    ]


def change_markers(repo: Path, base: str, head: str) -> dict[str, dict[str, object]]:
    """Return the markers a landing must show survived, keyed by changed path.

    The change is read from the merge base of ``base`` and ``head``, not from
    ``base`` itself: by the time a head is landed the base has usually moved
    with a promotion's own ledger commits, and a diff against it would attribute
    those commits to the change — a path the promotion added would read as one
    the change deleted, and a correct landing would be refused.

    A path the head deleted is recorded as deleted, so the landing is refused if
    it is present again; every other changed path carries the lines the head
    introduced. A removed line counts as a marker only when it occurred exactly
    once in the base copy: a line that appeared twice there is still present in
    the merged result, so requiring its absence would refuse a landing that
    behaved correctly.

    A path whose head copy is binary carries no lines — its blob id is recorded
    instead, and the landing finds that same blob to rule the change survived.
    """
    base = _merge_base(repo, base, head)
    markers: dict[str, dict[str, object]] = {}
    for _status, path in _changed_paths(repo, base, head):
        head_reading = _read_path(repo, head, path)
        if not head_reading["present"]:
            markers[path] = {
                "deleted": True,
                "binary": False,
                "blob": None,
                "introduced": [],
                "removed": [],
            }
            continue
        head_text = head_reading["text"]
        if head_text is None:
            markers[path] = {
                "deleted": False,
                "binary": True,
                "blob": head_reading["blob"],
                "introduced": [],
                "removed": [],
            }
            continue
        base_text = _read_path(repo, base, path)["text"] or ""
        markers[path] = {
            "deleted": False,
            "binary": False,
            "blob": head_reading["blob"],
            "introduced": [
                line for line in _marker_lines(head_text) if line not in base_text
            ],
            "removed": [
                line
                for line in _marker_lines(base_text)
                if line not in head_text and base_text.count(line) == 1
            ],
        }
    return markers


def surviving_change_refusals(
    repo: Path, revision: str, markers: dict[str, dict[str, object]]
) -> list[str]:
    """Return one refusal for each path whose change did not survive ``revision``.

    Each path is read through :func:`_read_path` against the head's own blob id.
    A path whose blob at ``revision`` equals the head's survived, whether it is
    text or binary, and its content is never read. A path whose
    blob differs was altered by the merge: one whose content is not text is
    refused because it cannot be compared line by line, and a text one is
    compared by the markers it was expected to carry.

    Each path that survives has its judgement recorded on its marker record
    under ``judgement`` — ``blob_identity`` when the blob matched and the
    content went unread, ``markers`` when the content was compared, and
    ``absent`` for a deleted path the merge correctly left gone — so the landing
    can report how each change was judged rather than only a marker count.

    A seam rather than a branch: the whole decision about whether a merge kept
    a reviewed change is here, so a caller can present the landing over a
    dropped resolution and see this one refusal rather than a branch's worth of
    conflict markers written into the checkout.
    """
    refusals: list[str] = []
    for path, expected in markers.items():
        reading = _read_path(repo, revision, path, matches=expected.get("blob"))
        if expected.get("deleted"):
            if reading["present"]:
                refusals.append(
                    f"{path}: the change deleted it and the merge has it back"
                )
            else:
                expected["judgement"] = "absent"
            continue
        if not reading["present"]:
            refusals.append(f"{path}: the change wrote it and the merge dropped it")
            continue
        if reading["identical"]:
            expected["judgement"] = "blob_identity"
            continue
        if expected.get("binary") or reading["text"] is None:
            refusals.append(
                f"{path}: the merge's content is not the head's and is not text, "
                "so the change could not be compared line by line"
            )
            continue
        expected["judgement"] = "markers"
        content = reading["text"]
        missing = [line for line in expected["introduced"] if line not in content]
        if missing:
            refusals.append(
                f"{path}: the change's {missing[0]!r} did not survive the merge"
            )
            continue
        still_there = [line for line in expected["removed"] if line in content]
        if still_there:
            refusals.append(
                f"{path}: the change removed {still_there[0]!r} and the merge kept it"
            )
    return refusals


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


def _run_waiting_out_a_held_index_lock(
    repo: Path,
    step: Callable[[int], subprocess.CompletedProcess[str]],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_seconds: float = DEFAULT_RETRY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, subprocess.CompletedProcess[str]], None] | None = None,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run ``step`` until it succeeds, a lock is not what refused it, or it runs out.

    ``step`` is called with the number of the attempt, counting from one, and
    returns the result of one git invocation. A failed result that left ``repo``
    holding an ``index.lock`` is retried after ``retry_seconds``, up to
    ``attempts`` calls in total, so a lock that is never released ends in the
    caller's refusal rather than a wait that never ends. ``on_retry`` is called
    with the number of retries made so far and the result that failed, before
    each wait, which is where a caller records the retry.

    Returns the last result with the number of retries made, so a caller that
    reports a lock that outlasted the attempts and a caller that records its
    retries both read one count from one place. The lock is read from the
    checkout's own ``index.lock`` rather than from the failing command's text,
    because that file is what the next attempt waits for.
    """
    retries = 0
    result = step(1)
    while result.returncode != 0 and retries + 1 < attempts and _index_lock_held(repo):
        retries += 1
        if on_retry is not None:
            on_retry(retries, result)
        sleep(retry_seconds)
        result = step(retries + 1)
    return result, retries


def _current_branch(repo: Path) -> str:
    result = _run(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise MergeError("HEAD is detached; check out the primary branch first")
    return branch


def _remote_head(
    repo: Path, remote: str, branch: str, env: dict[str, str] | None = None
) -> str:
    result = _run(repo, "ls-remote", remote, f"refs/heads/{branch}", env=env)
    if result.returncode != 0 or not result.stdout.strip():
        raise MergeError(f"cannot read {remote}'s {branch}")
    return result.stdout.split()[0].strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        _run(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0
    )


def _first_parent(repo: Path, commit: str) -> str:
    parents = _parents(repo, commit)
    return parents[0] if parents else commit


def _port22_refused(text: str) -> bool:
    """Whether a failure reads as an SSH connection refused on port 22.

    The signal is Git's own ssh error, ``connect to host <h> port 22:
    Connection refused``, which is what a compute node reports for GitHub.
    """
    low = (text or "").lower()
    return "connection refused" in low and ("port 22" in low or ":22" in low)


def _port443_env() -> dict[str, str]:
    """The environment for the port-443 route.

    The caller's ``GIT_SSH_COMMAND`` is honoured when set — it already names
    the route they want — and the stated fallback is used when it is not.
    """
    env = dict(os.environ)
    if not (env.get("GIT_SSH_COMMAND") or "").strip():
        env["GIT_SSH_COMMAND"] = FALLBACK_SSH_COMMAND
    return env


class _PushError(Exception):
    """The push did not land the commit on the remote."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _push_and_confirm(repo: Path, remote: str, commit: str, branch: str) -> str:
    """Push ``commit`` to ``branch`` and return the remote head it confirms.

    A refused port 22 is retried over the port-443 route, and the route that
    carried the push is reused for the read-back, so a host that reaches GitHub
    only on 443 confirms the remote head through the same route.
    """
    env: dict[str, str] | None = None
    result = _run(repo, "push", remote, f"{commit}:refs/heads/{branch}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if _port22_refused(detail):
            env = _port443_env()
            result = _run(
                repo, "push", remote, f"{commit}:refs/heads/{branch}", env=env
            )
            detail = result.stderr.strip() or result.stdout.strip()
        if result.returncode != 0:
            raise _PushError(f"push of {commit} to {remote} failed: {detail}")
    remote_head = _remote_head(repo, remote, branch, env=env)
    if remote_head != commit:
        raise _PushError(
            f"{remote}'s {branch} reads {remote_head}, not the landed {commit}"
        )
    return remote_head


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
    verify: Callable[[Path, str, str, str], None] | None = None,
) -> dict:
    """Land ``head`` on the checked-out branch and push it, returning a report.

    The base is pinned to ``HEAD``, the merge is built with ``merge-tree`` and
    ``commit-tree``, and the branch moves with ``--ff-only``, which completes or
    leaves the checkout untouched. A held ``index.lock`` or a moved ``HEAD`` is
    retried after a short pause, and the commit is rebuilt from the fresh HEAD so
    the fast-forward stays a fast-forward. With ``candidate`` the already-built
    merge is landed unchanged, provided its parents are the pinned HEAD and the
    head.

    ``verify`` is the seam a caller judges the merge through before it becomes
    reachable: it is called with the checkout, the pinned base, the head and the
    built commit, once per attempt and before the branch moves. A caller that
    refuses raises :class:`MergeError` from it, and because nothing has moved at
    that point the refusal has touched neither the branch nor the remote. The
    call is skipped on the already-landed path below, where the change is on the
    branch already and no merge is built.

    A head already in ``HEAD`` is treated as a landed merge whose push is
    outstanding: it is pushed rather than merged a second time. A push that
    fails after the fast-forward raises :class:`MergePushOutstandingError`, whose
    report names the landed commit and the base.
    """
    repo = Path(repo)
    head_sha = _rev_parse(repo, head)
    branch = _current_branch(repo)

    if _is_ancestor(repo, head_sha, "HEAD"):
        # A previous run landed this head and only its push is outstanding, so
        # the merge is already in HEAD: push it again rather than build a second
        # merge of the same head.
        landed = _rev_parse(repo, "HEAD")
        base = _first_parent(repo, landed)
    else:
        if candidate is None:
            _compose_message(message, body)
        base = commit = ""

        def step(_attempt: int) -> subprocess.CompletedProcess[str]:
            """Rebuild the merge, run a caller's check on it, and try to land it."""
            nonlocal base, commit
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

            # The seam runs before the branch moves, so what a caller refuses
            # here has touched neither the branch nor the remote. It is inside
            # the step because a retry rebuilds the commit against a fresh HEAD
            # and the new commit is what must be judged.
            if verify is not None:
                verify(repo, base, head_sha, commit)

            return _run(repo, "merge", "--ff-only", commit)

        remaining = attempts
        while remaining > 0:
            result, retries = _run_waiting_out_a_held_index_lock(
                repo,
                step,
                attempts=remaining,
                retry_seconds=retry_seconds,
                sleep=sleep,
            )
            remaining -= retries + 1
            if result.returncode == 0:
                break
            if _index_lock_held(repo):
                raise MergeError(
                    "the checkout's index.lock stayed held for "
                    f"{attempts} attempts; nothing was written"
                )
            if _rev_parse(repo, "HEAD") != base:
                if remaining <= 0:
                    raise MergeError("HEAD moved on each attempt; nothing was written")
                continue
            raise MergeError(
                "could not fast-forward: "
                + (result.stderr.strip() or result.stdout.strip())
            )
        else:  # pragma: no cover - every path above raises or breaks
            raise MergeError(f"could not land after {attempts} attempts")

        landed = _rev_parse(repo, commit)

    report = {
        "base": base,
        "head": head_sha,
        "commit": landed,
        "branch": branch,
        "remote": remote,
        "pushed": False,
    }
    try:
        remote_head = _push_and_confirm(repo, remote, landed, branch)
    except _PushError as exc:
        raise MergePushOutstandingError(report, exc.detail) from exc
    report["remote_head"] = remote_head
    report["pushed"] = True
    return report


def land(
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
    """Land ``head``, confirm its change survived the merge, and push it.

    The merge is built out of objects exactly as :func:`land_merge` builds it,
    and one step is added between building it and moving the branch: the merged
    tree must carry a marker the change introduced and lack any marker it
    removed. The check reads the tree the merge commit will put on the primary
    branch, so a resolution that drops the reviewed change is refused there,
    before the branch moves and before any push is attempted — the checkout is
    left exactly as it was and the remote is untouched.

    A head already in ``HEAD`` is a landing whose push is outstanding: it is
    pushed rather than merged a second time, and no marker is re-checked,
    because the change is already on the branch. The report carries a
    ``survived`` block naming the changed paths, the markers the check read, and
    each path's ``judgements`` — ``blob_identity`` for a path the merge carried
    unchanged, ``markers`` for a text path compared line by line, and ``absent``
    for a deletion the merge left gone — so a landing whose only change is
    binary reports the blob it checked rather than an empty marker count.
    """
    repo = Path(repo)
    head_sha = _rev_parse(repo, str(head))
    markers: dict[str, dict[str, object]] = {}

    def _refuse_a_dropped_change(
        checkout: Path, base: str, merged_head: str, commit: str
    ) -> None:
        """Judge the built merge by the tree it will put on the branch."""
        nonlocal markers
        markers = change_markers(checkout, base, merged_head)
        dropped = surviving_change_refusals(checkout, commit, markers)
        if dropped:
            raise MergeError(
                "the merge dropped the reviewed change, so nothing was moved "
                "or pushed: " + "; ".join(dropped)
            )

    report = land_merge(
        repo,
        head=head_sha,
        message=message,
        body=body,
        candidate=candidate,
        remote=remote,
        attempts=attempts,
        retry_seconds=retry_seconds,
        sleep=sleep,
        verify=_refuse_a_dropped_change,
    )
    report["survived"] = {
        "checked": bool(markers),
        "paths": sorted(markers),
        "markers": sum(
            len(expected["introduced"]) + len(expected["removed"])
            for expected in markers.values()
        ),
        "judgements": {
            path: markers[path].get("judgement") for path in sorted(markers)
        },
    }
    return report
