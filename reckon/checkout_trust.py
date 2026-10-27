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
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from reckon.crew.obligation_snapshot import FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS
from reckon.git_head import _head_identity

# The one proof that a checkout's current code loads is defined in
# ``reckon.crew_follow_commands`` as ``_FOLLOWER_RELOAD_PROBE``: the follower
# runs that exact text, and a reload proof spliced into that module at run time
# must be the one the follower itself runs. It is fetched lazily here so the
# module that defines it can import this one without a cycle.
IMPORT_PROBE_TIMEOUT = FOLLOWER_RELOAD_PROBE_TIMEOUT_SECONDS
COMMITTED_TREE_RETENTION = 3

# Whether a published image imports is a fact about the image and the
# interpreter that would load it, not about the process asking: every follower
# that reloads onto one image asks the same question of the same bytes at the
# same moment, and a herd of them would otherwise run the same thirty-second
# proof together. The first asker records the verdict beside the image, keyed by
# the interpreter, and every later asker reads it; an exclusive lock in the
# image's own directory serialises the proofs, so only one asker spends a probe.
#
# The lock is tried once and never waited on. A follower asks on its delivery
# path, and on a node whose shared filesystem is congested one proof can take
# longer than any bound a waiter could afford: every waiter then stopped
# streaming for the whole bound and printed a deferral line about a proof it
# was not running. An asker that finds the lock held is told the proof is
# pending and reads the recorded verdict at its next check instead.
IMPORT_VERDICT_PREFIX = ".import-verdict-"
IMPORT_PROBE_LOCK = ".import-probe.lock"

# Both the verdict and the lock live inside the image directory, which
# :func:`_images_ranked_for_pruning` orders by directory mtime. Writing either
# moves that mtime, so an old image that is merely being proved can rank with
# the fresh ones. The effect is bounded and can only retain an image: pruning
# decides what it must keep from the in-use and completion markers, never from
# the directory mtime, so the cost of the coupling is at worst one more image
# kept on disk than the directory mtime alone would have chosen. Filing these
# two files outside the image instead would strand them: removing the image
# directory is what removes its verdict and its lock, so a record kept anywhere
# else outlives the tree it describes and is read as a verdict about an image
# that no longer exists.

# A real code identity is a git object id: a 40-digit tree, or a 64-digit
# content digest for a package outside Git. This value collides with neither, so
# it is the reading the identity computation yields when a read it depends on did
# not answer at all. A failed read is not an absent catalogue: a process
# comparing identities must treat this reading as no new identity rather than as
# a value that moved, so nothing materialises an image from a partial read.
IDENTITY_UNKNOWN = "unknown"

# Every long-running reckon process -- the followers, the producers, the MCP
# server and queue admission -- asks once a second whether the code has moved,
# and each read of the identity runs ``git ls-tree`` twice and one ``git mktree``
# per composed tree. ``git mktree`` freshens an object that already exists by
# rewriting its mtime, so every reader on every node rewrites one shared inode
# every second with no code having moved. The identity is a function of what
# ``HEAD`` names, so the reading of this process's own checkout is held here
# against ``reckon.git_head``'s reading of ``HEAD`` and the ref it names
# as files without spawning git, and the git reads run only when that reading
# moves.
_identity_hold: dict[str, tuple[tuple, str]] = {}


class IdentityReadError(Exception):
    """A git read the identity depends on did not answer.

    Raised where a read could not be completed -- a timeout, an ``OSError``, or
    a subprocess that failed to run -- so the computation can tell that apart
    from a read that answered negatively. A read that ran and answered no, such
    as a checkout carrying no catalogue, is not a failure and does not raise.
    """


# Pruning keeps the newest images and removes the rest, but two states must
# never be removed whatever the ranking says.
#
# A materialiser publishes a tree by rename and takes its lease a moment
# later, so between those two steps the tree is complete, ranked old, and
# holds nothing a pruner can see: it is removable, and a concurrent
# materialiser does remove it. A tree published within the grace below whose
# lease has not been taken yet is exactly that gap, and is skipped.
#
# The cache is node-local, so every process that takes or renews an image's
# lease runs on this node and every pruner here sees its ``flock``. An image
# never outlives the node that published it, so nothing has to record which
# node holds one and no cross-node record is kept.
#
# The grace must cover a HEAD-churn window, not just the instant of the
# rename: only ``materialize_committed_tree`` writes a tree's directory mtime,
# through the ``os.utime(target)`` below, so during churn every follower that
# materialises a head touches that head's tree and a tree published moments ago
# can rank past the newest few. Measured on 2026-10-17: four commits in 137 s.
# The grace is set above that window with margin, so a genuinely idle tree is
# still reclaimed.
COMMITTED_TREE_PUBLISH_GRACE_SECONDS = 300.0

# How often one process renews the in-use mark of a held image. Every follower
# and producer on a node holds the same image, and renewing on each poll stamped
# one shared inode several times a second per process, which queues every
# holder behind the others' write tokens on a shared filesystem. A renewal per
# minute keeps the mark inside the publish grace a pruner reads.
COMMITTED_TREE_LEASE_RENEW_SECONDS = 60.0
_LEASE_RENEWED_AT: dict[int, float] = {}

# The model catalogue is versioned data beside the package rather than inside
# it, so a reload image carries it explicitly at the checkout's own relative
# path: a process reloaded into the image then resolves it through
# ``model_catalogue_path`` exactly as a checkout would, so a reloaded follower
# sizes its pane from the declared aliases and a reloaded producer stamps the
# model it dispatched onto its run. Only this one file is placed -- the
# ``docs/state`` tree beside it holds the ledger and run state, which are
# neither code nor read by a reloaded process.
CATALOGUE_RELATIVE_PATH = "docs/state/reckon/model-catalogue.yaml"


def _marker_age_seconds(path: Path) -> float | None:
    """Seconds since ``path`` was last modified, or None if it is not there."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _prune_must_keep(entry: Path) -> bool:
    """Whether pruning must leave a published tree alone.

    A tree carrying an in-use mark is one a reader may be importing from. The
    cache is node-local, so that reader runs on this node and its ``flock`` is
    visible here: a held lock refuses the pruner outright, and a mark no lock
    covers is left to the pruner rather than spared on recency alone.

    A tree with no in-use mark is one lying in the publish-to-lease gap, where
    an image is complete and ranked old but no pin has opened its mark yet, so
    no lock exists to refuse the pruner. It is left alone while its completion
    marker is younger than the grace, which covers the HEAD-churn window in
    which a tree published moments ago can rank past the newest few.
    """
    if (entry / ".in-use").exists():
        return False
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
        # The marker alone is the test, rather than
        # :func:`published_reload_image`, because this listing must see every
        # published entry in the cache and the recogniser additionally requires
        # the content-id name. The recogniser's files -- the proof's verdict and
        # its lock -- are written inside the image, so ranking an image by this
        # directory's mtime can be moved by a proof that is being run. Pruning
        # decides what it must keep from the in-use and completion markers
        # rather than from this mtime, so the coupling can only rank an image as
        # fresher than it is, and never cause an image in use to be removed.
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


def committed_reckon_tree(root: Path) -> str | None:
    """Read the id of the ``reckon/`` tree at the source checkout's HEAD.

    A process's code identity names the content of the package, not the commit
    that carries it. A commit touching only files outside ``reckon/`` -- a plan
    edit, a run-state write, a promotion record -- leaves this id unchanged, so
    it moves no follower or producer and extracts no reload tree. Git names a
    tree by its content, so two commits with the same package read alike
    however many commits sit between them.

    A git that cannot be run, or that answers no -- no such tree, or no
    repository at all -- returns None, which the caller reads as a checkout
    outside Git and identifies by its package content stamp instead. This
    reader does not distinguish a failed read from an absent package; the
    catalogue reader in ``_committed_catalogue_blob`` is the one that does.
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
    if result.returncode != 0:
        return None
    tree = result.stdout.strip()
    if len(tree) != 40:
        return None
    return tree


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
    carries no ``docs/``. ``ls-tree`` is asked rather than ``rev-parse`` because
    it answers whether or not the path exists: it exits zero both when the path
    is present, naming its blob, and when it is absent, naming nothing, and
    exits non-zero only when it could not answer at all. So empty output is
    git's own statement that ``HEAD`` carries no such path, and a read that did
    not complete is raised as unknown rather than counted as absence.
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-tree",
                "HEAD",
                "--",
                CATALOGUE_RELATIVE_PATH,
            ],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IdentityReadError(
            f"reading {CATALOGUE_RELATIVE_PATH} did not complete: {exc}"
        ) from exc
    if result.returncode != 0:
        raise IdentityReadError(
            f"reading {CATALOGUE_RELATIVE_PATH} could not be answered"
        )
    lines = result.stdout.strip().splitlines()
    if not lines:
        return None
    fields = lines[0].split("\t", 1)[0].split()
    blob = fields[2] if len(fields) == 3 else ""
    if len(blob) != 40:
        raise IdentityReadError(
            f"reading {CATALOGUE_RELATIVE_PATH} answered no usable blob"
        )
    return blob


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

    Only the catalogue's own read answers with the failure that earns an
    unknown identity (see ``_committed_catalogue_blob``): that read runs only
    after the listing below has answered for the package and scripts, so it
    describes a checkout that should have answered. Everything else -- a git
    that cannot be run, a checkout with no repository or no ``HEAD``, or a
    tree this process cannot compose because it may not write to the object
    store -- returns None, which the caller reads as a checkout outside Git
    and identifies by its package content stamp instead. That keeps a
    checkout this process merely cannot read through git on the identity it
    had before, rather than reporting it unknown and stopping every reader
    that would otherwise follow it.
    """
    try:
        entries = subprocess.run(
            ["git", "-C", str(root), "ls-tree", "HEAD", "reckon", "skills"],
            capture_output=True,
            text=True,
            timeout=IMPORT_PROBE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if entries.returncode != 0:
        return None
    names = {line.rsplit("\t", 1)[-1] for line in entries.stdout.splitlines()}
    if "skills" not in names:
        return committed_reckon_tree(root)
    if names != {"reckon", "skills"}:
        return None
    blob = _committed_catalogue_blob(root)
    if blob is None:
        tree = _mktree(root, entries.stdout)
    else:
        tree = _reload_tree_with_catalogue(root, entries.stdout, blob)
    return tree


def source_code_identity(package_dir: Path, *, source_root: Path | None = None) -> str:
    """Identify the package and scripts, or hash a package outside Git.

    A catalogue read that did not complete, on a checkout whose package and
    scripts git answered for, yields :data:`IDENTITY_UNKNOWN` rather than a
    partial identity: a failed read is not an absent catalogue, and a caller
    comparing identities must treat this reading as no new identity rather
    than as a moved one. A checkout git cannot answer for at all is not
    unknown; it is a checkout outside Git, which the computation answers for
    with None and which is identified by its package content stamp.

    A reading is held against :func:`reckon.git_head._head_identity`, the same
    spawn-free reading of ``HEAD`` and the ref it names that
    :mod:`reckon.change_streams` already holds its stream against, so the git
    reads run only when the commit ``HEAD`` resolves to can have moved. A new
    commit rewrites the loose ref and so moves that reading; ``git pack-refs``
    removes the loose ref, and the ref's presence is part of the identity, so
    folding it away moves the reading too. Only the checkout this process runs
    from is held -- the root resolved here when no other is named. A caller that
    names a source root is interrogating a tree that may have moved a moment
    before the call, and is answered from that tree on every read. A read that
    did not complete is never held either: it answers :data:`IDENTITY_UNKNOWN`
    and the next read runs git, so a failure is re-reported rather than frozen
    into a hold. A checkout outside Git has no such file to watch and keeps its
    content-stamp behaviour on every read.
    """
    own_root = source_root is None
    if source_root is None:
        source_root = Path(
            os.environ.get("RECKON_FOLLOWER_SOURCE_ROOT", str(package_dir.parent))
        )
    stamp = _head_identity(source_root) if own_root else None
    if stamp is not None:
        held = _identity_hold.get(str(source_root))
        if held is not None and held[0] == stamp:
            return held[1]
    try:
        tree = _committed_reload_tree(source_root)
    except IdentityReadError:
        return IDENTITY_UNKNOWN
    if tree is not None:
        if stamp is not None:
            _identity_hold[str(source_root)] = (stamp, tree)
        return tree
    from reckon.crew.obligation_snapshot import code_stamp

    return code_stamp(package_dir)


def running_code_identity(package_dir: Path) -> str:
    """Identify the image this process loaded, even if source HEAD moved."""
    carried = os.environ.get("RECKON_FOLLOWER_IMAGE_COMMIT", "")
    if re.fullmatch(r"[0-9a-f]{40}", carried):
        return carried
    image_root = package_dir.parent
    if published_reload_image(image_root):
        return image_root.name
    return source_code_identity(package_dir)


def _committed_tree_cache() -> Path:
    """The reload image cache: under this node's runtime root, never the home.

    Every process on the node resolves its images from one directory, so a
    ``flock`` taken on a tree by one of them is visible to every pruner here and an
    image never travels between nodes. The root is the station's own, read through
    :func:`reckon.crew.station_store.runtime_root_source` rather than a second
    resolver, so the cache and the station cannot disagree about where the
    node's runtime state lives, and its filesystem-type proof is the station's
    too: a root on a shared filesystem is refused there, in the station's own
    words, rather than falling through to a cache where ``flock`` does not
    cross nodes.
    """
    from reckon.crew import station_store

    root, _source = station_store.runtime_root_source()
    if root is None:
        raise RuntimeError(
            "no node-local runtime root is available: the reload image cache "
            "lives under XDG_RUNTIME_DIR, /run/user/<uid> or TMPDIR, never the "
            "crew home"
        )
    station_store._refuse_nonlocal_root(root, mounts=None)
    return root / "reload-trees"


def pin_matches_materialized_tree(descriptor: int, root: Path) -> bool:
    """A carried lease must still name the published image's lock inode."""
    if not root.is_dir() or not published_reload_image(root):
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
    _LEASE_RENEWED_AT[descriptor] = time.monotonic()
    return descriptor


def refresh_materialized_tree_pin(descriptor: int, root: Path) -> None:
    """Renew the held image lease, at most once per renewal interval.

    A follower or producer calls this on every poll; only a call at least
    :data:`COMMITTED_TREE_LEASE_RENEW_SECONDS` after this descriptor's last
    renewal writes anything. The mark's mtime is the characteristics a pruner
    reads to know a pin has run here recently, and it is stamped on the
    descriptor the pin already holds.

    A tree removed from under this process by a pruner is a tree with no lease
    left to renew: the descriptor names an inode with no directory left to hold
    it, and the poll that renews here has a reload ahead of it that materialises
    a fresh tree. So the stamp is attempted rather than owed, and it is not
    allowed to end the watch, which is what a follower serving a session has to
    survive.
    """
    moment = time.monotonic()
    renewed = _LEASE_RENEWED_AT.get(descriptor)
    if renewed is not None and moment - renewed < COMMITTED_TREE_LEASE_RENEW_SECONDS:
        return
    _LEASE_RENEWED_AT[descriptor] = moment
    with contextlib.suppress(OSError):
        os.utime(descriptor, None)


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


class ImportProofPending(str):
    """Another process holds the image's proof lock and has not answered yet.

    Not a refusal of the image and not a timeout: nothing about the image or
    the node has been learned. A reader that only asks whether the code loads
    still gets a refusal for now; the follower tells this apart by type, says
    nothing, and reads the recorded verdict on its next check.
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


def published_reload_image(root: Path) -> bool:
    """Whether ``root`` is a published reload image rather than a source tree.

    A published image is named by the content id of the package it holds and
    carries the completion marker its materialiser writes, so it is immutable:
    the code it holds cannot change under an asker, and a verdict recorded for
    it stays true until the image is pruned. A source checkout carries no such
    marker, so its content can move between one asker and the next and it keeps
    the per-process proof.

    This is the single recogniser for the distinction: every caller that needs
    to know whether a process is running from, or about to import, a published
    image asks here rather than testing for the completion marker itself, so a
    change to what publication means moves one predicate rather than several.
    """
    if re.fullmatch(r"[0-9a-f]{40}", root.name) is None:
        return False
    try:
        return (root / ".complete").is_file()
    except OSError:
        return False


def _import_verdict_interpreter_key() -> str:
    """A short digest naming the interpreter a verdict was proven under.

    Whether a tree imports is a fact about the tree *and* the interpreter that
    would load it: two interpreters carrying different dependencies answer
    differently about the same tree, so a verdict is filed under the one that
    produced it and never reused for another.
    """
    text = (
        f"{os.path.realpath(sys.executable)}\0"
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _import_verdict_path(root: Path) -> Path:
    return root / f"{IMPORT_VERDICT_PREFIX}{_import_verdict_interpreter_key()}"


def _read_import_verdict(path: Path) -> tuple[bool, str] | None:
    """The verdict a previous asker recorded, or None when none is readable.

    A verdict that cannot be read -- absent, removed by a prune between the
    listing and the open, or written by another revision of this module -- is no
    verdict at all, so the asker proves the tree itself rather than treating an
    unreadable record as a refusal.
    """
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    imports = record.get("ok")
    reason = record.get("reason", "")
    if not isinstance(imports, bool) or not isinstance(reason, str):
        return None
    return imports, reason


def _record_import_verdict(root: Path, *, imports: bool, reason: str) -> None:
    """Publish an image's verdict beside it, through the shared atomic writer.

    The writer renames a whole temporary into place, so no asker ever reads a
    half-written verdict. The verdict is a cache an asker can rebuild by proving
    the image again, so it is not made durable before the rename. A record that
    cannot be written costs a later asker its probe and nothing else, so the
    failure is swallowed rather than allowed to end the asker that has just
    proved the image.
    """
    from reckon._store import write_atomically

    payload = json.dumps({"ok": imports, "reason": reason})
    with contextlib.suppress(OSError):
        write_atomically(
            _import_verdict_path(root),
            lambda handle: handle.write(payload),
            fsync=False,
            mode=0o600,
        )


def _probe_imports(root: Path) -> tuple[bool, str]:
    """Run the import proof in a throwaway interpreter, recording nothing."""
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


def _take_import_probe_lock(descriptor: int) -> bool:
    """Take the image's proof lock if it is free, without waiting for it."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _published_image_imports(root: Path) -> tuple[bool, str]:
    """Prove a published image once, under its lock, and share the verdict.

    The verdict is read first, so an asker arriving after another has proved the
    image pays nothing. When none is recorded the asker tries the image's
    exclusive lock once. Holding it, the asker reads again -- the holder before
    it may have just recorded the verdict -- and only then runs the proof,
    recording a pass and an import failure alike. A timeout is never recorded,
    because it says nothing about the image: the next asker proves it alone.

    An asker that finds the lock held reads the verdict one last time and, with
    none, returns :class:`ImportProofPending` at once. It neither waits for the
    holder nor proves the image itself, so a slow proof costs its one prover and
    no one else.
    """
    verdict_path = _import_verdict_path(root)
    remembered = _read_import_verdict(verdict_path)
    if remembered is not None:
        return remembered
    try:
        descriptor = os.open(root / IMPORT_PROBE_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        # No lock to serialise on -- an image a pruner removed between the read
        # and this open, or one this process may not write to -- so the proof is
        # run without one rather than skipped.
        return _probe_imports(root)
    try:
        if not _take_import_probe_lock(descriptor):
            remembered = _read_import_verdict(verdict_path)
            if remembered is None:
                return False, ImportProofPending(
                    "another process is proving this image; its verdict is read "
                    "at the next check"
                )
            return remembered
        remembered = _read_import_verdict(verdict_path)
        if remembered is not None:
            return remembered
        imports, reason = _probe_imports(root)
        if isinstance(reason, ImportProbeTimedOut):
            return imports, reason
        _record_import_verdict(root, imports=imports, reason=reason)
        return imports, reason
    finally:
        os.close(descriptor)


def reckon_imports(root: Path) -> tuple[bool, str]:
    """Whether the checkout's current code loads in a throwaway interpreter.

    The reason returned with a refusal is an :class:`ImportProbeTimedOut` when
    the probe ran out of its bound rather than answering, an
    :class:`ImportProofPending` when another process is proving the same image,
    and a line of the failed import's own output otherwise. All three refuse:
    neither a timeout nor a pending proof shows that the image loads, whatever
    the reader does with the distinction.

    A published reload image is proved once, under an exclusive lock in the
    image's own directory, and the verdict is recorded beside it keyed by the
    interpreter, so concurrent askers on one image start one probe between them
    and all read its answer. A source checkout has no immutable tree, so its
    content can move between askers and it keeps the per-process proof.
    """
    if published_reload_image(root):
        return _published_image_imports(root)
    return _probe_imports(root)


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
