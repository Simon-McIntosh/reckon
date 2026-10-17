"""One check of whether a moved checkout may be loaded.

A code stamp that has moved says the checkout changed on disk; it does not say
the change is fit to run. Two readers must reach the same answer about that
tree. A follower or producer replaces its own image only onto a tree it can
trust, and the MCP's stale response tells its caller to reconnect onto the
checkout only when that same tree can be trusted. Held as separate probes the
two disagreed: on a half-edited tree the MCP pointed its caller at code the
followers refused to load, which is the tree that took five producers down.

The check has two conditions, in order. The tree must be clean under ``reckon/``
against ``HEAD``: a rename applied at a call site and not at its definition
imports cleanly and fails only at first use, so git is asked which tracked files
differ rather than the module being run. And every tracked module must import:
the probe compiles each one and imports it in a throwaway interpreter, because
the running process is the wrong place to discover that the new code does not
load.

The import probe's text is defined once, in ``reckon.crew_follow_commands`` as
``_FOLLOWER_RELOAD_PROBE``, and this module reads it rather than holding its own
copy, so the two readers share one definition. Both call
:func:`moved_checkout_is_trustworthy` rather than carrying a check of their own.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.obligation_snapshot import FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS

# The one proof that a checkout's current code loads is defined in
# ``reckon.crew_follow_commands`` as ``_FOLLOWER_RELOAD_PROBE``: the follower
# runs that exact text, and a reload proof spliced into that module at run time
# must be the one the follower itself runs. It is fetched lazily here so the
# module that defines it can import this one without a cycle.
IMPORT_PROBE_TIMEOUT = FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS
COMMITTED_TREE_RETENTION = 3


def committed_head(root: Path) -> str | None:
    """Read the source checkout's commit, independent of working-tree edits."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and len(commit) == 40 else None


def _committed_tree_cache() -> Path:
    from reckon.crew.obligation_snapshot import crew_home

    return crew_home() / "reload-trees"


def materialize_committed_tree(root: Path, commit: str) -> tuple[Path, list[Path]]:
    """Publish a package extracted from one commit and prune older images."""
    source_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    cache = _committed_tree_cache() / source_key
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / commit
    if not (target / ".complete").is_file():
        staging = Path(tempfile.mkdtemp(prefix=f".{commit}-", dir=cache))
        try:
            archive = subprocess.run(
                ["git", "-C", str(root), "archive", "--format=tar", commit, "reckon"],
                capture_output=True,
                timeout=IMPORT_PROBE_TIMEOUT,
                check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as stream:
                stream.extractall(staging, filter="data")
            if not (staging / "reckon" / "__init__.py").is_file():
                raise RuntimeError(f"commit {commit} contains no reckon package")
            (staging / ".complete").touch()
            try:
                os.rename(staging, target)
            except OSError:
                if not (target / ".complete").is_file():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    target.touch()
    images = sorted(
        (entry for entry in cache.iterdir() if (entry / ".complete").is_file()),
        key=lambda entry: entry.stat().st_mtime_ns,
        reverse=True,
    )
    removed = []
    for entry in images[COMMITTED_TREE_RETENTION:]:
        if entry == target:
            continue
        shutil.rmtree(entry)
        removed.append(entry)
    return target, removed


def _import_probe() -> str:
    """The shared import proof text, read from its single definition."""
    from reckon.crew_follow_commands import _FOLLOWER_RELOAD_PROBE

    return _FOLLOWER_RELOAD_PROBE


class ImportProbeTimedOut(str):
    """The probe ran out of its bound instead of answering.

    A probe that ran out of time and a probe that ran and failed both leave the
    image unproven, so both are reported as a refusal and every reader that only
    asks whether the code loads keeps its answer. They are not the same fact,
    though: a failed import is a property of the checkout and holds until the
    code changes, while a timeout is the instrument running out of budget under
    load on a checkout that may be perfectly loadable. A reader that must react
    differently -- the follower, which retries a timeout at its next check
    rather than waiting for the code to move -- tells the two apart by this
    type; every other reader takes it as the printable reason it also is.
    """


@dataclass(frozen=True)
class CheckoutTrust:
    """The answer: ``trusted``, and when it is false, which condition refused.

    ``dirty`` distinguishes the two refusals so a caller can say which one it
    is: true means a tracked file differs from ``HEAD`` and ``detail`` names the
    files; false with ``trusted`` false means the import proof refused and
    ``detail`` names the reason.
    """

    trusted: bool
    dirty: bool
    detail: str


def tracked_reckon_tree_is_dirty(root: Path) -> tuple[bool, str]:
    """Whether any tracked file under ``reckon/`` differs from ``HEAD``.

    A tree a reloader reads mid-edit can import cleanly and still fail at its
    first use: a rename half-applied binds a name the import proof never
    reaches. The committed image is what a replacement should run, so git is
    asked which tracked files differ from it rather than the answer being
    re-derived here.

    A tree git cannot read -- not a checkout, or no git on ``PATH`` -- is
    reported clean: the check has no opinion there and the import proof is still
    a guard, so a reader that cannot see a repository keeps its old behaviour
    rather than deferring every load forever. Untracked files are excluded by
    git itself; a newly written module that fails to import is caught by the
    import proof.
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain",
                "--untracked-files=no",
                "--",
                "reckon",
            ],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode != 0:
        return False, ""
    entries = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not entries:
        return False, ""
    return True, ", ".join(entries)


def reckon_imports(root: Path) -> tuple[bool, str]:
    """Whether the checkout's current code loads in a throwaway interpreter.

    The reason returned with a refusal is an :class:`ImportProbeTimedOut` when
    the probe ran out of its bound rather than answering, and a line of the
    failed import's own output otherwise. Both still refuse: a timeout is not a
    proof that the image loads, whatever the reader does with the distinction.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _import_probe(), str(root)],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, ImportProbeTimedOut(
            f"the import probe timed out after {exc.timeout:g} s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode == 0:
        return True, ""
    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    return False, (lines[-1] if lines else f"exit {completed.returncode}")


def moved_checkout_is_trustworthy(root: Path) -> CheckoutTrust:
    """Whether a checkout whose code has moved may be loaded.

    Clean under ``reckon/`` against ``HEAD`` and importable: both, and the tree
    is trusted. The dirty check is asked first and short-circuits -- a dirty
    tree is not fit to run whatever its imports say, and the import proof, which
    loads every module, is the expensive half.
    """
    dirty, detail = tracked_reckon_tree_is_dirty(root)
    if dirty:
        return CheckoutTrust(trusted=False, dirty=True, detail=detail)
    imports, reason = reckon_imports(root)
    if not imports:
        return CheckoutTrust(trusted=False, dirty=False, detail=reason)
    return CheckoutTrust(trusted=True, dirty=False, detail="")
