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

import contextlib
import fcntl
import hashlib
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
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

# Pruning keeps the newest images and removes the rest, but two states must
# never be removed whatever the ranking says.
#
# A materialiser publishes a tree by rename and takes its lease a moment
# later, so between those two steps the tree is complete, ranked old, and
# holds nothing a pruner can see: it is removable, and a concurrent
# materialiser does remove it. A tree published within the grace below whose
# lease has not been taken yet is exactly that gap, and is skipped.
#
# A lease is a ``flock``, which does not cross nodes on a shared GPFS home, so
# a lease taken on another node is invisible here: a pruner here has no lock
# to be refused by and removes a tree another node is still importing from.
# Every node that takes or renews a lease records itself beside the tree, so a
# tree whose record names a node other than this one, renewed within the
# window below, is one still imported from somewhere else and keeps its tree.
# The record is what carries that fact; the lease file's own mtime cannot
# stand in for it, because this filesystem stamps a lease taken moments ago
# and one released moments ago identically, and a released lease leaves the
# same file behind as a held one.
#
# The grace must cover a HEAD-churn window, not just the instant of the
# rename: only ``materialize_committed_tree`` writes a tree's directory mtime,
# through the ``os.utime(target)`` below, so during churn every follower that
# materialises a head touches that head's tree and a tree published moments ago
# can rank past the newest few. Measured on 2026-10-17: four commits in 137 s.
# The grace is set above that window with margin, and stays well inside the
# lease window so a genuinely idle tree is still reclaimed.
COMMITTED_TREE_PUBLISH_GRACE_SECONDS = 300.0
COMMITTED_TREE_LEASE_WINDOW_SECONDS = 600.0
COMMITTED_TREE_LEASE_MARKER = ".lease"

# The model catalogue is versioned data beside the package rather than inside
# it, so a reload image carries it explicitly at the checkout's own relative
# path: a process reloaded into the image then resolves it through
# ``model_catalogue_path`` exactly as a checkout would, so a reloaded follower
# sizes its pane from the declared aliases and a reloaded producer stamps the
# model it dispatched onto its run. Only this one file is placed -- the
# ``docs/state`` tree beside it holds the ledger and run state, which are
# neither code nor read by a reloaded process.
CATALOGUE_RELATIVE_PATH = "docs/state/reckon/model-catalogue.yaml"


def _lock_node_name() -> str:
    """The name this node records for itself in a lease record.

    ``flock`` is per node, so two nodes sharing a home are two lock domains
    and the name is how one node's lease is told from another's.
    """
    return socket.gethostname()


def _marker_age_seconds(path: Path) -> float | None:
    """Seconds since ``path`` was last modified, or None if it is not there."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _lease_nodes(root: Path) -> set[str]:
    """The nodes whose lease record stands beside this image."""
    try:
        text = (root / COMMITTED_TREE_LEASE_MARKER).read_text()
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def _record_lease_node(root: Path) -> None:
    """Name this node in the image's lease record.

    The write also stamps the record's mtime, which is the renewal a pruner on
    another node reads. Names accumulate rather than replace, so a node that
    only renews a lease taken by another does not erase the taker.
    """
    nodes = _lease_nodes(root) | {_lock_node_name()}
    (root / COMMITTED_TREE_LEASE_MARKER).write_text("\n".join(sorted(nodes)) + "\n")


def _prune_must_keep(entry: Path) -> bool:
    """Whether pruning must leave a published tree alone.

    A tree whose lease record names a node other than this one, and was
    renewed within the lease window, is one a node that is not this one is
    importing from: the tree is held, and no lock here would say so.

    A tree carrying an in-use mark and no record at all is one in the instant
    between a pin creating that mark and the same pin writing its record, where
    the mark exists before a name does. Its own recency is what says a pin is
    in flight, and a pruner reading the entry in between would otherwise find
    no name to refuse it and take a tree a node is taking; a mark old enough
    that no pin could still be writing is let go.

    A tree with no in-use mark is one lying in the publish-to-lease gap, and is
    left alone while its completion marker is younger than the grace.
    """
    named = _lease_nodes(entry)
    if named - {_lock_node_name()}:
        age = _marker_age_seconds(entry / COMMITTED_TREE_LEASE_MARKER)
        if age is not None and age < COMMITTED_TREE_LEASE_WINDOW_SECONDS:
            return True
        # The record names only hosts that have stopped renewing, but a pin on
        # yet another node can be in flight: it opens and stamps the in-use
        # mark before it takes the flock and rewrites the record, so in that
        # instant the record still names only the stale host. This node cannot
        # see that pin's flock (flock does not cross nodes), so the mark's
        # mtime -- stamped the instant the pin is taken and on every renewal --
        # is the only evidence it is in use, and a mark inside the grace holds
        # the tree.
        in_use_age = _marker_age_seconds(entry / ".in-use")
        return (
            in_use_age is not None and in_use_age < COMMITTED_TREE_PUBLISH_GRACE_SECONDS
        )
    if (entry / ".in-use").exists():
        if named:
            return False
        age = _marker_age_seconds(entry / ".in-use")
        return age is not None and age < COMMITTED_TREE_PUBLISH_GRACE_SECONDS
    age = _marker_age_seconds(entry / ".complete")
    return age is not None and age < COMMITTED_TREE_PUBLISH_GRACE_SECONDS


def _images_ranked_for_pruning(cache: Path) -> list[Path]:
    """The published images, newest directory mtime first.

    A peer prunes the same cache concurrently, so an image present in the
    listing can be gone by the time its mtime is read. That is not an error:
    the tree is no longer there to rank, and the prune of the tree now being
    materialised does not depend on it, so it is skipped.
    """
    ranked: list[tuple[int, Path]] = []
    for entry in cache.iterdir():
        if not (entry / ".complete").is_file():
            continue
        try:
            ranked.append((entry.stat().st_mtime_ns, entry))
        except (FileNotFoundError, NotADirectoryError):
            continue
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in ranked]


def _open_prune_lease(entry: Path) -> int | None:
    """Open the lock a pruner takes on an image, or None if it has vanished.

    The image can be removed by a peer pruner between the ranking and this
    open. A tree a peer already removed counts as removed, so the caller is
    told there is nothing left to lease rather than being raised at.
    """
    try:
        return os.open(entry / ".in-use", os.O_CREAT | os.O_RDWR, 0o600)
    except (FileNotFoundError, NotADirectoryError):
        return None


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


def committed_reckon_tree(root: Path) -> str | None:
    """Read the id of the ``reckon/`` tree at the source checkout's HEAD.

    A process's code identity names the content of the package, not the commit
    that carries it. A commit touching only files outside ``reckon/`` -- a plan
    edit, a run-state write, a promotion record -- leaves this id unchanged, so
    it moves no follower or producer and extracts no reload tree. Git names a
    tree by its content, so two commits with the same package read alike
    however many commits sit between them.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD:reckon"],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and len(tree) == 40 else None


def _mktree(root: Path, entries: str) -> str | None:
    """Write one tree object from ``ls-tree``-shaped entry lines, or None."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "mktree"],
            input=entries,
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and len(tree) == 40 else None


def _committed_catalogue_blob(root: Path) -> str | None:
    """The catalogue blob at ``HEAD``, or None when the checkout carries none.

    The catalogue is data a checkout may legitimately lack -- a wheel install
    carries no ``docs/``.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--verify",
                f"HEAD:{CATALOGUE_RELATIVE_PATH}",
            ],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    blob = result.stdout.strip()
    return blob if result.returncode == 0 and len(blob) == 40 else None


def _reload_tree_with_catalogue(root: Path, base_entries: str, blob: str) -> str | None:
    """Fold a tree entry for the catalogue into the package and scripts.

    ``mktree`` composes one directory level at a time, so the catalogue's
    directory path is built from the leaf outward: a tree holding the blob,
    then one holding that tree, and so on to ``docs``, which joins the package
    and scripts in the top-level tree. Only the catalogue's own file is placed,
    so the ledger and run state beside it is not carried.
    """
    parts = CATALOGUE_RELATIVE_PATH.split("/")
    entry = f"100644 blob {blob}\t{parts[-1]}\n"
    for segment in reversed(parts[:-1]):
        tree = _mktree(root, entry)
        if tree is None:
            return None
        entry = f"040000 tree {tree}\t{segment}\n"
    return _mktree(root, base_entries + entry)


def _committed_reload_tree(root: Path) -> str | None:
    """Write a content tree of the package, its dispatch scripts and catalogue.

    The catalogue folds into the identity with the package and scripts, so a
    catalogue edit reloads processes to read the new models and an edit outside
    the three moves nothing. A checkout carrying no catalogue yields the
    package-and-scripts tree unchanged.
    """
    try:
        entries = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "HEAD", "reckon", "skills"],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
        if entries.returncode != 0:
            return None
        names = {line.rsplit("\t", 1)[-1] for line in entries.stdout.splitlines()}
        if "skills" not in names:
            return committed_reckon_tree(root)
        if names != {"reckon", "skills"}:
            return None
    except (OSError, subprocess.SubprocessError):
        return None
    blob = _committed_catalogue_blob(root)
    if blob is None:
        return _mktree(root, entries.stdout)
    return _reload_tree_with_catalogue(root, entries.stdout, blob)


def source_code_identity(package_dir: Path, *, source_root: Path | None = None) -> str:
    """Identify the package and scripts, or hash a package outside Git."""
    if source_root is None:
        source_root = Path(
            os.environ.get("RECKON_FOLLOWER_SOURCE_ROOT", str(package_dir.parent))
        )
    tree = _committed_reload_tree(source_root)
    if tree is not None:
        return tree
    from reckon.crew.obligation_snapshot import code_stamp

    return code_stamp(package_dir)


def running_code_identity(package_dir: Path) -> str:
    """Identify the image this process loaded, even if source HEAD moved."""
    carried = os.environ.get("RECKON_FOLLOWER_IMAGE_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", carried):
        return carried
    image_root = package_dir.parent
    if (image_root / ".complete").is_file() and re.fullmatch(
        r"[0-9a-f]{40}", image_root.name
    ):
        return image_root.name
    return source_code_identity(package_dir)


def _committed_tree_cache() -> Path:
    from reckon.crew.obligation_snapshot import crew_home

    return crew_home() / "reload-trees"


def pin_matches_materialized_tree(descriptor: int, root: Path) -> bool:
    """A carried lease must still name the published image's lock inode."""
    if not root.is_dir() or not (root / ".complete").is_file():
        return False
    try:
        held = os.fstat(descriptor)
        published = (root / ".in-use").stat()
    except OSError:
        return False
    return (held.st_dev, held.st_ino) == (published.st_dev, published.st_ino)


def pin_materialized_tree(root: Path) -> int:
    """Hold a kernel lease while this process may import from an image.

    The pin stamps the in-use mark as it takes the lease, before the flock and
    the record write. A pruner on another node cannot see this node's flock,
    and until the record is rewritten the tree's record can still name only a
    host that has stopped renewing, so the mark's mtime is the one thing that
    says a pin is in progress across the gap between those two steps. Opening
    an existing mark rewrites nothing, so the stamp is made explicit here.
    """
    descriptor = os.open(root / ".in-use", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.utime(descriptor, None)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
    except BaseException:
        os.close(descriptor)
        raise
    if not pin_matches_materialized_tree(descriptor, root):
        os.close(descriptor)
        raise RuntimeError(f"materialized tree vanished before its lease: {root}")
    _record_lease_node(root)
    return descriptor


def refresh_materialized_tree_pin(descriptor: int, root: Path) -> None:
    """Renew the held image lease on every follower or producer poll.

    The lease itself is renewed on the descriptor and the node's name in the
    tree's lease record is re-stamped, which is the renewal a pruner on
    another node reads: that node cannot see this one's lock, so the record is
    the only evidence it has that the tree is still imported from.

    A tree removed from under this process by a pruner elsewhere is a tree
    with no lease left to renew and no record left to stamp, and neither half
    of the renewal is then owed to anyone: the descriptor names an inode with
    no directory left to hold it, and the poll that renews here has a reload
    ahead of it that materialises a fresh tree. So both halves are attempted
    and neither is allowed to end the watch, which is what a follower serving
    a session has to survive.
    """
    with contextlib.suppress(OSError):
        os.utime(descriptor, None)
    with contextlib.suppress(OSError):
        _record_lease_node(root)


def _reckon_subtree(root: Path, revision: str) -> str:
    """Resolve a commit or a ``reckon/`` tree id to the subtree git can archive.

    An identity names the ``reckon/`` tree object itself, whose contents are the
    package's files without their ``reckon/`` directory. Archiving a commit
    instead yields the whole repository tree, so a commit -- still carried by an
    image armed before the identity named the package -- is resolved to its
    ``reckon/`` subtree first. A revision that is already that subtree, or that
    cannot be resolved, is returned unchanged.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{revision}:reckon"],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return revision
    tree = result.stdout.strip()
    return tree if result.returncode == 0 and len(tree) == 40 else revision


def _archive_source(root: Path, revision: str) -> tuple[str, str]:
    """Select the paired image tree, or adapt a legacy package or commit id."""
    try:
        kind = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-t", revision],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
        if kind.returncode == 0 and kind.stdout.strip() == "tree":
            package = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", f"{revision}:reckon"],
                capture_output=True,
                text=True,
                timeout=IMPORT_PROBE_TIMEOUT,
                check=False,
            )
            if package.returncode == 0:
                return revision, ""
    except (OSError, subprocess.SubprocessError):
        pass
    return _reckon_subtree(root, revision), "reckon/"


def materialize_committed_tree(
    root: Path, revision: str, *, active_root: Path | None = None
) -> tuple[Path, list[Path], list[Path], int]:
    """Publish package and dispatch scripts from one identity; prune old images.

    A current identity names a tree with ``reckon/`` and ``skills/``. Older
    package-tree and commit identities still unpack under ``reckon/``. The
    directory name carries the identity read by ``running_code_identity``.
    """
    source_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    cache = _committed_tree_cache() / source_key
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / revision
    if not (target / ".complete").is_file():
        staging = Path(tempfile.mkdtemp(prefix=f".{revision}-", dir=cache))
        try:
            tree, prefix = _archive_source(root, revision)
            archive = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "archive",
                    "--format=tar",
                    f"--prefix={prefix}",
                    tree,
                ],
                capture_output=True,
                timeout=IMPORT_PROBE_TIMEOUT,
                check=True,
            )
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as stream:
                stream.extractall(staging, filter="data")
            if not (staging / "reckon" / "__init__.py").is_file():
                raise RuntimeError(f"identity {revision} contains no reckon package")
            (staging / ".complete").touch()
            try:
                os.rename(staging, target)
            except OSError:
                if not (target / ".complete").is_file():
                    raise
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
            raise RuntimeError(f"cannot unpack identity {revision}: {exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    pin = pin_materialized_tree(target)
    try:
        os.utime(target, None)
        images = _images_ranked_for_pruning(cache)
        removed = []
        kept = []
        for entry in images[COMMITTED_TREE_RETENTION:]:
            if entry == target:
                continue
            if entry == active_root:
                kept.append(entry)
                continue
            if _prune_must_keep(entry):
                kept.append(entry)
                continue
            descriptor = _open_prune_lease(entry)
            if descriptor is None:
                removed.append(entry)
                continue
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    kept.append(entry)
                    continue
                # A peer pruner can remove the same entry during this walk: the
                # tree is gone either way, so it counts as removed and the
                # reload of the tree being materialised is not aborted.
                with contextlib.suppress(FileNotFoundError, NotADirectoryError):
                    shutil.rmtree(entry)
                removed.append(entry)
            finally:
                os.close(descriptor)
    except BaseException:
        os.close(pin)
        raise
    return target, removed, kept, pin


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
    """Whether any tracked package or dispatch script differs from ``HEAD``.

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
                "skills",
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
