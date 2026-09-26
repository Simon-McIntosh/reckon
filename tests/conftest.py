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
import tempfile
import time
from pathlib import Path

import pytest

from reckon.crew.dispatch import WATCH_ARMING_ENV
from reckon.crew.routing import signal_worker

ARMING_MARKER = "arms_watch_producer"

# How long a producer the reaper signalled is given to exit before its survival
# is read as a leak. The producer is a Python process with no signal handler, so
# the default disposition ends it promptly; the grace exists so a loaded host
# cannot make a terminated producer read as one that ignored the signal.
_REAP_GRACE_SECONDS = 15.0

# Prefixes of throwaway configuration homes tests created outside the pytest
# base temp directory. A home the pytest tree does not contain cannot be found by
# searching that tree, so its name is recorded instead: a producer still naming a
# home with one of these prefixes is this run's leak. Prefixes rather than paths
# because the test removes the directory and a leaked producer goes on naming the
# deleted path.
_TEST_TEMP_HOME_PREFIXES: set[str] = set()

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

# The environment a crew dispatch exports into the process it launches: the run
# it belongs to, that run's manifest, and when the attempt started. Every one is
# a fact about the worker, not about the code under test, and a suite that
# inherits it reads it as one. Measured with RECKON_RUN_ID exported, as it is in
# every worker's shell: 66 of the 79 tests in tests/test_edit_plan.py fail,
# because a plan write with no checkout_path is resolved against a run that is
# not the test's and the write is refused. Removed for every test; a test whose
# subject IS the run scope sets what it needs for itself.
_DISPATCH_IDENTITY_ENV = (
    "RECKON_RUN_ID",
    "RECKON_MANIFEST",
    "RECKON_ATTEMPT_STARTED_AT",
)


@pytest.fixture(autouse=True)
def without_dispatch_identity(monkeypatch):
    """No test inherits the identity of the worker that ran it."""
    for name in _DISPATCH_IDENTITY_ENV:
        monkeypatch.delenv(name, raising=False)


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

    Reaping by record is not enough on its own. A record names a producer that
    reached its seat; a producer that died earlier, or one started by a path
    that never wrote a record, is not listed and would survive the reaper in
    silence. So the reaped pids are given a bounded grace to exit and then every
    live ``crew watch`` process naming a home this run created is read from
    ``/proc`` and the session is failed on it, by pid and by home.
    """
    root = tmp_path_factory.getbasetemp()
    yield
    reaped = reapable_watch_pids(root)
    for pid in reaped:
        signal_worker(pid, signal.SIGTERM)
    await_exit(reaped)
    leaks = leaked_watch_producers(root)
    if leaks:
        named = ", ".join(f"pid {pid} (RECKON_HOME={home})" for pid, home in leaks)
        pytest.fail(
            "crew watch producers this session armed outlived it: "
            f"{named}. They poll a temporary configuration home the suite is "
            "about to remove; a test that arms a producer must reap it, and the "
            "session fixture's reaper must be able to see it."
        )


def await_exit(pids: list[int], grace: float = _REAP_GRACE_SECONDS) -> None:
    """Wait, bounded, for every pid in ``pids`` to disappear from ``/proc``.

    ``/proc`` rather than ``os.kill(pid, 0)``: a signalled process can linger as
    a zombie only relative to its own parent, and the pids here are detached,
    so their absence from ``/proc`` is the fact that matters.
    """
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not any(Path("/proc", str(pid)).exists() for pid in pids):
            return
        time.sleep(0.05)


def _live_watch_producers() -> list[tuple[int, Path]]:
    """Every live ``crew watch`` process, with the home its environment names.

    Read from ``/proc`` by argv and environment, never by a command-line
    pattern alone: a pattern matches any process carrying the words, including a
    peer's producer and the shell that ran the scan, while the process's own
    environment names the one home it reports into.
    """
    found: list[tuple[int, Path]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"watch" not in argv or b"--project" not in argv:
            continue
        home = _environ_config_home(environ)
        if home is not None:
            found.append((int(entry.name), home))
    return found


def _environ_config_home(environ: list[bytes]) -> Path | None:
    for item in environ:
        name, _, value = item.partition(b"=")
        if name == b"RECKON_HOME" and value:
            return Path(os.fsdecode(value))
    return None


def test_temp_config_home(prefix: str) -> Path:
    """A throwaway configuration home a test created, registered for attribution.

    Some tests must place a home where the arming guard does not look — the one
    that shows arming proceeding for an ordinary home, and the one that shows a
    record under another home is refused. Those cannot live under the pytest base
    temp directory, because that directory is exactly what the guard treats as a
    throwaway. Recording the prefix keeps them attributable anyway: a producer
    still naming one after the reaper ran is this run's leak even though the
    directory sat outside the base temp tree and the test has since removed it.
    """
    _TEST_TEMP_HOME_PREFIXES.add(prefix)
    return Path(tempfile.mkdtemp(prefix=prefix))


def _home_is_test_owned(home: Path, base: Path) -> bool:
    """True when ``home`` is a configuration home this run created."""
    try:
        resolved = home.resolve()
    except OSError:
        resolved = home
    if resolved == base or base in resolved.parents:
        return True
    temp = Path(tempfile.gettempdir())
    if home.parent == temp:
        return any(home.name.startswith(prefix) for prefix in _TEST_TEMP_HOME_PREFIXES)
    return False


def producers_naming_home(home: Path) -> list[tuple[int, Path]]:
    """Live ``crew watch`` producers whose own environment names ``home``.

    The binding a caller asserts against is the configuration home, not a
    project or an argv pattern: under a parallel run a peer's producer for the
    same project is a different fact, and a scan that cannot tell the two apart
    fails on whatever else the host happens to be running.
    """
    try:
        wanted = home.resolve()
    except OSError:
        wanted = home
    return [
        (pid, named)
        for pid, named in _live_watch_producers()
        if _resolve(named) == wanted
    ]


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def leaked_watch_producers(base: Path) -> list[tuple[int, Path]]:
    """Live ``crew watch`` producers still naming a home this run created.

    A stored seat record is a claim about the past — the pid it names may since
    have exited and been reused — so liveness is read from the process itself.
    A producer naming an ordinary home, or one belonging to a peer session, is
    not this run's and is left alone.
    """
    return [
        (pid, home)
        for pid, home in _live_watch_producers()
        if _home_is_test_owned(home, base)
    ]


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
