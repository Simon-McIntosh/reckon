from __future__ import annotations

import contextlib
import os
import select
import socket
import struct
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from reckon import metadata_index
from reckon.env_duration import read_duration_override
from reckon.git_dates import _file_identity, _git_directory, _head_identity

_INOTIFY_EVENT = struct.Struct("iIII")
_INOTIFY_CHANGE_MASK = 0x00000FCC
_INOTIFY_DIRECTORY = 0x40000000
_CHANGE_SETTLE_S = 0.25


class _ProjectChangeWatch:
    """Block on kernel filesystem notifications for one mounted docs tree.

    A watch may also carry a ``wake_fd``, the read end of a pipe the fleet
    watch writes to when its cross-node detector reports a change to this tree.
    The select below then has two change sources — the kernel for a local write
    and the pipe for a write another node made — through one handler.
    """

    def __init__(self, root: Path, *, wake_fd: int | None = None) -> None:
        import ctypes

        self.root = root.resolve()
        self._wake_fd = wake_fd
        self._libc = ctypes.CDLL(None, use_errno=True)
        initializer = getattr(self._libc, "inotify_init1", None)
        add_watch = getattr(self._libc, "inotify_add_watch", None)
        if initializer is None or add_watch is None:
            raise OSError("filesystem change notifications are unavailable")
        self.fd = initializer(os.O_CLOEXEC)
        if self.fd < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self._add_watch = add_watch
        self._directories: dict[int, Path] = {}
        try:
            self._watch_tree(self.root)
        except Exception:
            os.close(self.fd)
            raise

    def _watch_tree(self, root: Path) -> None:
        for directory, subdirectories, _files in os.walk(root):
            self._watch(Path(directory))
            subdirectories[:] = [
                name for name in subdirectories if name not in {".git", "node_modules"}
            ]

    def _watch(self, directory: Path) -> None:
        encoded = os.fsencode(directory)
        descriptor = self._add_watch(self.fd, encoded, _INOTIFY_CHANGE_MASK)
        if descriptor < 0:
            import ctypes

            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(directory))
        self._directories[descriptor] = directory

    def consume(self) -> bool:
        """Read pending notifications, arming a watch on any new directory."""
        events = os.read(self.fd, 64 * 1024)
        offset = 0
        while offset + _INOTIFY_EVENT.size <= len(events):
            descriptor, mask, _cookie, name_length = _INOTIFY_EVENT.unpack_from(
                events, offset
            )
            offset += _INOTIFY_EVENT.size
            name = events[offset : offset + name_length].rstrip(b"\0")
            offset += name_length
            if mask & _INOTIFY_DIRECTORY and name:
                candidate = self._directories.get(descriptor, self.root) / os.fsdecode(
                    name
                )
                if candidate.is_dir():
                    self._watch_tree(candidate)
        return True

    def wait(self, connection: socket.socket) -> bool:
        """Return false when the browser disconnects, true on a tree change."""
        sources = [self.fd, connection]
        if self._wake_fd is not None:
            sources.append(self._wake_fd)
        readable, _, _ = select.select(sources, [], [])
        if connection in readable:
            try:
                if not connection.recv(1, socket.MSG_PEEK):
                    return False
            except (ConnectionError, OSError):
                return False
        local = self.fd in readable
        woke = self._wake_fd is not None and self._wake_fd in readable
        if not local and not woke:
            return False
        if local:
            self.consume()
        if woke:
            self._drain_wake()
        return True

    def _drain_wake(self) -> None:
        """Empty the detector's pipe so the next change writes a fresh byte."""

        try:
            os.read(self._wake_fd, 4096)
        except OSError:
            return

    def drain(self, settle_s: float) -> None:
        """Consume further events until none arrive for ``settle_s`` seconds."""

        while True:
            readable, _, _ = select.select([self.fd], [], [], settle_s)
            if not readable:
                return
            try:
                os.read(self.fd, 64 * 1024)
            except OSError:
                return

    def close(self) -> None:
        os.close(self.fd)


class _TreeWake:
    """One open change stream's pipe for waking on a cross-node write.

    The stream owns the pipe's lifetime: it registers when it opens and closes
    both ends when it closes, and the fleet watch only writes a byte to the
    writer end whenever it reports a change to that tree.
    """

    def __init__(self, watch: _FleetChangeWatch, root: Path) -> None:
        self._watch = watch
        self.root = root
        self.reader, self.writer = os.pipe()

    @property
    def fd(self) -> int:
        return self.reader

    def close(self) -> None:
        self._watch.drop_subscription(self)
        for descriptor in (self.reader, self.writer):
            with contextlib.suppress(OSError):
                os.close(descriptor)


class _TreeSignal:
    """The last identities the detector read for one tree.

    Each is replaced as the signal fires, so a change fires once and the next
    pass is quiet until something moves again. ``armed`` is set on the first
    pass, which only records the current identities: a pass has no baseline to
    compare against until then.
    """

    def __init__(self, project: str, root: Path) -> None:
        self.project = project
        self.root = root
        self.armed = False
        self.dir_stamps: dict[str, list[int]] = {}
        self.index_identity: tuple[int, int, int] | None = None
        self.git_identity: tuple | None = None
        self.slow_signature: tuple[int, int] | None = None


#: How often the cross-node detector stats each tree's directories and git
#: index, and how often it sweeps the covered files' mtimes. The directory and
#: git signals are a stat per directory, so the fast pass is cheap; the slow
#: sweep reads a stat per covered file, so it runs only for a tree with a
#: recent reader or an open stream.
_CROSS_NODE_FAST_S = 10.0
_CROSS_NODE_SLOW_S = 60.0
#: How long a read keeps a tree worth the slow sweep after the reader stops.
_CROSS_NODE_READ_WINDOW_S = 600.0


def _cross_node_fast_s() -> float:
    return read_duration_override(
        "RECKON_CROSS_NODE_FAST_S", _CROSS_NODE_FAST_S, 0, True
    )


def _cross_node_slow_s() -> float:
    return read_duration_override(
        "RECKON_CROSS_NODE_SLOW_S", _CROSS_NODE_SLOW_S, 0, True
    )


def _cross_node_read_window_s() -> float:
    return read_duration_override(
        "RECKON_CROSS_NODE_READ_WINDOW_S", _CROSS_NODE_READ_WINDOW_S, 0, True
    )


def _persisted_index_identity(root: Path, project: str) -> tuple[int, int, int] | None:
    """The identity of a tree's persisted index file, or None when absent."""

    if not project:
        return None
    try:
        return _file_identity(metadata_index._index_path(root, project))
    except OSError:
        return None


def _git_signal_identity(tree: Path) -> tuple | None:
    """The git-side identity whose movement means a commit or an add.

    The resolved HEAD identity paired with the index file's own identity: the
    index file moves on every add, commit, merge and pull from any node, which
    is the signal a cross-node commit leaves. HEAD identity is read as files,
    never by running git, so it costs no process and leaves the ``_git_head``
    memo undisturbed.
    """

    repo_dir = tree.parent
    head = _head_identity(repo_dir)
    if head is None:
        return None
    git_dir = _git_directory(repo_dir)
    index_identity = _file_identity(git_dir / "index") if git_dir is not None else None
    return (head, index_identity)


class _FleetChangeWatch:
    """Watch every mounted docs tree for the life of the served process.

    Each tree's watch is armed on the background reader thread, one tree at a
    time, rather than before the thread starts: arming a tree walks it, and a
    walk of a shared-filesystem tree can take seconds, so arming every tree up
    front keeps the process from binding its port until the last walk is done.
    The reader thread arms a tree and only then selects across the descriptors
    armed so far, and a tree is covered by the discovery reuse window until its
    watch is armed.

    A kernel notification on one tree drops that tree's memoised walk at once,
    so the reuse window never has to hide a change the kernel reported; it only
    backstops writes the kernel does not report, such as a write from another
    login node on the shared filesystem. One thread selects across every tree's
    notification descriptor, so a change to any mounted tree is seen without
    walking anything.

    Inotify reports only writes made on the same node, and on a shared
    filesystem every write from a compute node is invisible to it, so a second
    thread runs a cross-node detector beside the event loop. The detector stats
    each tree's directories and git index on a short interval, and sweeps the
    covered files' mtimes on a longer one for a tree that has an open stream or
    a recent reader. When it sees a change the kernel did not report it does
    what an event does: it drops the tree's derived views and wakes every open
    change stream through that tree's registered pipe.
    """

    def __init__(self, mounts: Mapping[str, Path]) -> None:
        # The project beside each path, because the detector resolves a tree's
        # persisted index stamps and its git directory from the pair.
        self._projects = {
            Path(path).resolve(): str(project) for project, path in mounts.items()
        }
        self._trees = sorted(self._projects)
        self._watches: dict[int, _ProjectChangeWatch] = {}
        self._subscribers: dict[Path, list[_TreeWake]] = {}
        self._read_at: dict[Path, float] = {}
        self._signals: dict[Path, _TreeSignal] = {}
        self._fleet_versions: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop_reader, self._stop_writer = os.pipe()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._detector_thread: threading.Thread | None = None
        self.running = False

    def start(self) -> _FleetChangeWatch:
        """Begin watching every mounted tree; return self for chaining."""
        if self.running:
            return self
        # `running` is set before the threads start so the arming loop, which
        # reads it to notice a concurrent close, cannot run ahead of it.
        self.running = True
        self._thread = threading.Thread(
            target=self._run, name="reckon-tree-watch", daemon=True
        )
        self._thread.start()
        self._detector_thread = threading.Thread(
            target=self._detector_run, name="reckon-tree-detect", daemon=True
        )
        self._detector_thread.start()
        return self

    # ── Cross-node write subscriptions ────────────────────────────────────

    def subscribe(self, root: Path) -> _TreeWake | None:
        """Register an open change stream's wake pipe for one tree."""

        root = Path(root).resolve()
        if not self.running:
            return None
        wake = _TreeWake(self, root)
        with self._lock:
            self._subscribers.setdefault(root, []).append(wake)
        return wake

    def drop_subscription(self, wake: _TreeWake) -> None:
        with self._lock:
            peers = self._subscribers.get(wake.root)
            if peers is not None:
                with contextlib.suppress(ValueError):
                    peers.remove(wake)
                if not peers:
                    self._subscribers.pop(wake.root, None)

    def note_read(self, root: Path) -> None:
        """Stamp a tree as read, so the slow sweep keeps watching it a while."""

        with self._lock:
            self._read_at[Path(root).resolve()] = time.monotonic()

    def notify(self, root: Path) -> None:
        """Wake every open stream registered for one tree."""

        root = Path(root).resolve()
        with self._lock:
            wakes = list(self._subscribers.get(root, ()))
        for wake in wakes:
            with contextlib.suppress(OSError):
                os.write(wake.writer, b"x")

    def publish_fleet(self, key: tuple[str, ...], version: str) -> None:
        """Record a fleet-scoped result's new version and wake every stream.

        A result derived over every mount belongs to no one project, so every
        open stream is woken and each writes its own event for a key whose
        version moved past the one it last sent.
        """

        token = ":".join(key)
        with self._lock:
            self._fleet_versions[token] = version
            wakes = [wake for peers in self._subscribers.values() for wake in peers]
        for wake in wakes:
            with contextlib.suppress(OSError):
                os.write(wake.writer, b"x")

    def fleet_versions(self) -> dict[str, str]:
        """The published version of every fleet-scoped result, by key."""

        with self._lock:
            return dict(self._fleet_versions)

    def _spawn_changes(self, tree: Path) -> None:
        """Start the refreshes a change to one tree calls for.

        The watch computes nothing itself: it starts the project's discovery
        refresh and the one fleet-rollup refresh, both single-flight, so a
        change to several projects at once starts one rollup child.
        """

        from reckon import serve

        project = self._projects.get(Path(tree).resolve(), "")
        if project:
            serve._start_refresh(("discover", project))
        serve._start_refresh(("rollup",))

    # ── The cross-node detector ───────────────────────────────────────────

    def _detector_run(self) -> None:
        fast_s = max(_cross_node_fast_s(), 0.05)
        slow_s = max(_cross_node_slow_s(), 0.05)
        next_fast = 0.0
        next_slow = 0.0
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_fast:
                next_fast = now + fast_s
                self._fast_pass()
            if now >= next_slow:
                next_slow = now + slow_s
                self._slow_pass()
            delay = min(next_fast, next_slow) - time.monotonic()
            if delay > 0:
                self._stop_event.wait(delay)

    def _signal_for(self, tree: Path) -> _TreeSignal:
        signal = self._signals.get(tree)
        if signal is None:
            signal = _TreeSignal(self._projects.get(tree, ""), tree)
            self._signals[tree] = signal
        return signal

    def _fast_pass(self) -> None:
        from reckon.serve import LOGGER

        for tree in self._trees:
            if not self.running:
                return
            try:
                self._fast_tree(tree)
            except OSError as exc:
                LOGGER.warning("Cross-node fast pass over %s failed: %s", tree, exc)

    def _fast_tree(self, tree: Path) -> None:
        """Compare directory and git identities against the cached stamps.

        The persisted index file is written lazily, so a rebuild is learned
        from that file's identity: when it moves, the cached stamps are stale
        and are reloaded. A directory whose identity has moved is replaced with
        the identity just read before the tree is invalidated, so one change
        fires once and the next pass is quiet until something changes again.
        """
        from reckon.serve import _invalidate_tree_views

        signal = self._signal_for(tree)
        index_identity = _persisted_index_identity(tree, signal.project)
        if not signal.armed:
            signal.armed = True
            signal.index_identity = index_identity
            signal.dir_stamps = dict(
                metadata_index._load_dir_stamps(tree, signal.project)
            )
            signal.git_identity = _git_signal_identity(tree)
            return
        if index_identity != signal.index_identity:
            signal.index_identity = index_identity
            fresh = metadata_index._load_dir_stamps(tree, signal.project)
            if fresh:
                signal.dir_stamps = dict(fresh)
        fired = False
        for relative, cached in list(signal.dir_stamps.items()):
            current = metadata_index._dir_identity(tree / relative)
            if current != cached:
                signal.dir_stamps[relative] = current
                fired = True
        git_identity = _git_signal_identity(tree)
        if git_identity != signal.git_identity:
            first = signal.git_identity is None
            signal.git_identity = git_identity
            if not first:
                fired = True
        if fired:
            _invalidate_tree_views(tree, changed_at=time.monotonic())
            self._spawn_changes(tree)
            self.notify(tree)

    def _slow_pass(self) -> None:
        from reckon.serve import LOGGER

        now = time.monotonic()
        for tree in self._trees:
            if not self.running:
                return
            if not self._tree_is_active(tree, now):
                continue
            try:
                self._slow_tree(tree)
            except OSError as exc:
                LOGGER.warning("Cross-node slow pass over %s failed: %s", tree, exc)

    def _slow_tree(self, tree: Path) -> None:
        """Sweep the covered files' count and newest mtime for one tree."""
        from reckon.serve import (
            _STATE_ROOT,
            _invalidate_tree_views,
            _walk_discovery_signature,
        )

        signal = self._signal_for(tree)
        signature = _walk_discovery_signature(tree, signal.project, _STATE_ROOT)
        if signal.slow_signature is None:
            signal.slow_signature = signature
            return
        if signature != signal.slow_signature:
            signal.slow_signature = signature
            _invalidate_tree_views(tree, changed_at=time.monotonic())
            self._spawn_changes(tree)
            self.notify(tree)

    def _tree_is_active(self, tree: Path, now: float) -> bool:
        with self._lock:
            if self._subscribers.get(tree):
                return True
            read_at = self._read_at.get(tree)
        if read_at is None:
            return False
        return now - read_at < _cross_node_read_window_s()

    def _arm_every_tree(self) -> None:
        """Arm one tree at a time, registering each as its watch is built."""
        from reckon.serve import LOGGER

        for tree in self._trees:
            if not self.running:
                return
            try:
                watch = _ProjectChangeWatch(tree)
            except OSError as exc:
                LOGGER.warning("Not watching %s for changes: %s", tree, exc)
                continue
            with self._lock:
                if not self.running:
                    watch.close()
                    return
                self._watches[watch.fd] = watch

    def _run(self) -> None:
        from reckon.serve import _invalidate_tree_views

        self._arm_every_tree()
        while self._watches:
            readable, _, _ = select.select([*self._watches, self._stop_reader], [], [])
            if self._stop_reader in readable:
                return
            for descriptor in readable:
                watch = self._watches.get(descriptor)
                if watch is None:
                    continue
                watch.consume()
                # The stamp is taken as the change is observed, before the
                # settle wait, so a discovery another watcher recomputed for
                # the same event is reused rather than recomputed again.
                changed_at = time.monotonic()
                # A save or a merge is a burst of events; let it settle so the
                # burst costs one invalidation rather than one per event.
                watch.drain(_CHANGE_SETTLE_S)
                _invalidate_tree_views(watch.root, changed_at=changed_at)
                self._spawn_changes(watch.root)
                self.notify(watch.root)

    def close(self) -> None:
        """Signal both threads to stop and release their descriptors."""
        self.running = False
        self._stop_event.set()
        with contextlib.suppress(OSError):
            os.write(self._stop_writer, b"x")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        if self._detector_thread is not None:
            self._detector_thread.join(timeout=5.0)
        self._detector_thread = None
        with self._lock:
            for watch in self._watches.values():
                watch.close()
            self._watches.clear()
        for descriptor in (self._stop_reader, self._stop_writer):
            with contextlib.suppress(OSError):
                os.close(descriptor)
