"""Suite-wide isolation: no test reaches the real fleet, and none arms it.

Two facts made this file necessary. A test that dispatches arms a project
watch producer detached, which is right for a coordinator and wrong under a
test: the test ends and the producer survives with nothing left that knows it
exists. And two watch locks carrying test input names were found in the real
configuration home, so at least one path resolved the real home rather than a
fixture's temporary one. Both are closed here by default, in one place bound to
a moment that always happens — every test gets this fixture.

A test whose own subject is the producer lifecycle needs a real producer and
reaps what it starts. It says so with the ``arms_watch_producer`` marker; the
default is suppression, so arming is opted into rather than out of.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from reckon.crew.dispatch import WATCH_ARMING_ENV
from reckon.crew.routing import signal_worker

ARMING_MARKER = "arms_watch_producer"

# Modules whose subject IS the producer lifecycle: they start producers on
# purpose and terminate them in their own teardown. Everything else is
# suppressed. Prefer the marker on new tests; these entries carry the modules
# that predate it.
_PRODUCER_LIFECYCLE_MODULES = frozenset(
    {
        "test_crew",
        "test_crew_dispatch_arming",
        "test_crew_dispatch_guard",
        "test_crew_orphan",
        "test_crew_watch_lifetime",
        "test_crew_watch_stream",
        "test_crew_watch_visibility",
        "test_crew_watchlife",
    }
)


def watch_record_dirs(root: Path) -> list[Path]:
    """Watcher-record directories belonging to configuration homes under ``root``.

    Each producer writes its seat record — the pid it registered, under the lock
    it holds — into its own home's ``crew/watch`` directory, so the directory's
    location says which home the producer belongs to.
    """
    found = [
        candidate
        for candidate in root.rglob("watch")
        if candidate.is_dir() and candidate.parent.name == "crew"
    ]
    return sorted(set(found))


def watcher_record_pids(root: Path) -> list[tuple[int, Path]]:
    """Registered watch producer pids, with the home each record lies under.

    The pid comes from the record the producer wrote into its own configuration
    home, never from a scan of command lines. A command-line pattern matches any
    process carrying the words — a peer's producer, the test runner that spawned
    one, the shell that ran the scan — while a record names exactly one home and
    the process registered against it.
    """
    found: list[tuple[int, Path]] = []
    for directory in watch_record_dirs(root):
        home = directory.parent.parent
        for record in sorted(directory.glob("*.lock")):
            pid = _record_pid(record)
            if pid is not None:
                found.append((pid, home))
    return found


def _record_pid(record: Path) -> int | None:
    try:
        value = json.loads(record.read_text() or "{}")
    except (OSError, ValueError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _named_config_home(pid: int) -> Path | None:
    """The configuration home ``pid`` names in its own environment, if any."""
    try:
        environ = Path("/proc", str(pid), "environ").read_bytes().split(b"\0")
    except OSError:
        return None
    for item in environ:
        name, _, value = item.partition(b"=")
        if name == b"RECKON_HOME" and value:
            return Path(os.fsdecode(value))
    return None


def reapable_watch_pids(root: Path) -> list[int]:
    """Pids this run may terminate, from the records under ``root``.

    A record names the pid that registered it at the time it registered, which
    is a claim about the past: the number may since have been reused by an
    unrelated process. The process's own environment settles it. A pid whose
    environment names a configuration home other than the record's is refused,
    so a record that has drifted from the process it points at cannot turn the
    session teardown into a signal aimed at somebody else's watcher. A pid whose
    environment cannot be read is refused too — an unreadable environment is not
    evidence that the process is ours.
    """
    pids: list[int] = []
    for pid, home in watcher_record_pids(root):
        named = _named_config_home(pid)
        if named is None or named.resolve() != home.resolve():
            continue
        pids.append(pid)
    return pids


@pytest.fixture(scope="session", autouse=True)
def reaped_watch_producers(tmp_path_factory):
    """Nothing armed against this run's temporary homes outlives the run.

    Arming is detached by design, so a producer a test starts is not the
    test's child and no teardown of the test's own can be relied on to end it:
    a suite interrupted at a fence leaves its `finally` blocks unrun. This is
    the backstop for the tests that legitimately arm, and it is bound to the
    one moment that always happens.
    """
    root = tmp_path_factory.getbasetemp()
    yield
    for pid in reapable_watch_pids(root):
        signal_worker(pid, signal.SIGTERM)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{ARMING_MARKER}: the test owns and reaps a real watch producer",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    for item in items:
        module = getattr(item, "module", None)
        name = getattr(module, "__name__", "").rsplit(".", 1)[-1]
        if name in _PRODUCER_LIFECYCLE_MODULES:
            item.add_marker(getattr(pytest.mark, ARMING_MARKER))


@pytest.fixture(autouse=True)
def isolated_reckon_home(request, tmp_path_factory, monkeypatch):
    """Point the configuration home at a temporary tree and suppress arming."""
    home = tmp_path_factory.mktemp("reckon-home")
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv(
        WATCH_ARMING_ENV,
        "on" if request.node.get_closest_marker(ARMING_MARKER) else "off",
    )
    return home
