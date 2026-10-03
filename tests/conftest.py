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

import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

from reckon.crew.dispatch import WATCH_ARMING_ENV
from reckon.crew.routing import signal_worker

ARMING_MARKER = "arms_watch_producer"

# Promotion fixtures record a command that can run from any temporary
# repository; the interpreter path must exist in the process doing the check.
EXECUTABLE_GATE_COMMAND = f"{shlex.quote(sys.executable)} -c pass"

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

# Modules whose subject IS the model catalogue: they resolve against the real
# catalogue to assert what a catalogue layer supplies. Everything else must not
# read it, so the checkout's own catalogue cannot leak alias, effort, model,
# budget group or rates into a fixture host that declared none. A test that
# points ``RECKON_MODEL_CATALOGUE`` at its own fixture is unaffected by the
# default; this set carries only the modules that need the repository's file.
_CATALOGUE_SUBJECT_MODULES = frozenset({"test_flight_catalogue_layer"})

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


# The picker answers over OpenRouter and authenticates with a live credential.
# Since routing.picker became ``route`` by default, any dispatch that names no
# lane asks the live service; the whole suite then depends on an external
# service, spends money on every run, and reads its judgement back as a test
# outcome. Measured 2026-10-03: a whole-suite run failed
# ``test_a_layer_removing_a_default_leaves_it_writable_and_records_it`` with a
# real "BudgetHold: wave held on budget ... picker selected hold" — a live Jev
# verdict, not a fact about the code under test. Isolation is closed here, in
# one place bound to every test, and the credential file the checkout's real
# ``.env`` supplies is replaced by a path that holds no key, so credential
# resolution fails deterministically and the client raises ``LiveJevDisabled``
# before it builds a request.
@pytest.fixture(autouse=True)
def no_live_jev(request, tmp_path_factory, monkeypatch):
    """No test reaches the live picker service or reads the real credential."""
    from reckon.crew.picker import client

    monkeypatch.delenv("OPENROUTER_API_KEY_RECKON", raising=False)
    absent = tmp_path_factory.mktemp("no-live-jev") / "absent-credential"
    monkeypatch.setenv(client.CREDENTIAL_ENV, str(absent))
    module = getattr(getattr(request.node, "module", None), "__name__", "")
    if module.rsplit(".", 1)[-1] not in _CATALOGUE_SUBJECT_MODULES:
        # Repo state must not leak into a fixture host: the checkout's own
        # catalogue would otherwise fill alias, effort, model, budget group and
        # rates into any backend a test declares. A catalogue test keeps the
        # real file by name above, or points the override at its own fixture.
        monkeypatch.setenv(
            "RECKON_MODEL_CATALOGUE",
            str(tmp_path_factory.mktemp("no-catalogue") / "absent-catalogue.yaml"),
        )

    # Credential absence alone is not isolation: a test that sets the key back
    # builds and sends the request. Whatever credential is present, the client's
    # HTTP call is wrapped so a request aimed at the picker's endpoint raises the
    # same ``LiveJevDisabledError`` the missing credential would, while any other
    # host a test fetches — its own loopback server — still reaches the network
    # through the original call. A test that patches ``urllib.request.urlopen``
    # itself replaces this guard, so a test answering the call with a fixture
    # keeps working.
    original_urlopen = urllib.request.urlopen

    def refuse_live_jev(target, *args, **kwargs):
        url = getattr(target, "full_url", None)
        if url is None and isinstance(target, str):
            url = target
        if url and str(url).startswith(client.DECISIONS_ORIGIN):
            raise client.LiveJevDisabledError(
                "live Jev is disabled under test: the request to the decisions "
                "endpoint was refused before a connection was opened"
            )
        return original_urlopen(target, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", refuse_live_jev)


# The served process's discovery walk-reuse window. ``serve.main`` assigns
# ``reckon.serve._SIGNATURE_TTL_S`` on whichever thread runs the server and does
# not restore it, so a test that starts the served process leaves a live
# discovery memo behind for every later test sharing its process to read. A
# clearer refusal lives in ``tests/test_server_listens_before_it_watches.py``,
# which refuses at setup when it finds a memo it did not arm; this restores each
# test's entry value so that refusal is never triggered by a sibling.
#
# Armed only by the negative-control run: with it set, the restore is skipped
# and a served case's assignment survives into the next test in the process.
_SKIP_DISCOVERY_MEMO_RESTORE_ENV = "RECKON_TEST_SKIP_DISCOVERY_MEMO_RESTORE"


@pytest.fixture(autouse=True)
def restore_discovery_memo_ttl():
    """No test leaves the discovery walk-reuse window changed for the next."""
    if os.environ.get(_SKIP_DISCOVERY_MEMO_RESTORE_ENV) == "1":
        yield
        return
    from reckon import serve

    original = serve._SIGNATURE_TTL_S
    yield
    serve._SIGNATURE_TTL_S = original


# Scheduler verbs a test must never reach unless it put a working one on PATH
# itself. A test asserting on placement or dispatch that forgets to provide one
# reaches the host's real scheduler: measured 2026-09-30, an admitted-dispatch
# case minted a real allocation on every whole-suite run, because the ensure's
# ``salloc`` resolved through the ambient PATH. The guard below refuses that
# reach instead of letting the allocation happen, so the suite cannot hold the
# fleet's shared reservation as a side effect of being run.
_SCHEDULER_VERBS = ("salloc", "sbatch", "srun", "squeue", "scontrol", "scancel")

# Of those verbs, the ones whose reach fails the test: the verbs that mint an
# allocation or place a step inside one. A read-only probe (``squeue``,
# ``scontrol``) and a cancel are still refused by the stub and recorded, but no
# test is failed for reaching one: a test whose subject is a liveness probe may
# legitimately ask the host's scheduler whether a job is alive, and failing it
# would make this guard the cause of an unrelated red rather than a refusal of
# an allocation. The allocation verbs are the ones whose reach can hold a real
# reservation. ``srun`` is included because a bare step client with no job id
# mints an allocation of its own.
_ALLOCATION_VERBS = ("salloc", "sbatch", "srun")

# Where a reached verb is recorded. The guard stub appends its own argv and the
# test that reached it, so the failing message names both. An evidence run may
# point this at its own log to correlate conftest's refusal with the run's shim.
_SCHEDULER_REACH_LOG_ENV = "RECKON_SCHEDULER_REACH_LOG"

_REFUSING_SCHEDULER_STUB = """#!/bin/sh
name=${0##*/}
printf '%s\\t%s\\n' "${PYTEST_CURRENT_TEST:-<no-test>}" "$name $*" \\
  >> "$RECKON_SCHEDULER_REACH_LOG"
exit 1
"""


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


# Where a seat record is mirrored before the tree holding it is pruned. The
# record is the evidence a watcher was armed and the handle a session-end reap
# would use; keeping temporary directories only for failures would otherwise
# delete it the moment the arming test passes.
_SEAT_RECORDS_DIR = "_seat-records"


def preserve_seat_records(root: Path) -> None:
    """Copy every seat record out of the test trees ``root`` is about to prune.

    The copy keeps the ``crew/watch`` shape and the home's path relative to
    ``root``, so a reader looking for records under the run's root still finds
    them after the arming test's own directory is removed. The originals are
    left in place until the prune removes them, so a reap taken now still reads
    the home each record was actually written under.
    """
    mirror = root / _SEAT_RECORDS_DIR
    for directory in watch_record_dirs(root):
        if mirror in directory.parents:
            continue
        destination = mirror / directory.relative_to(root)
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for record in directory.glob("*.lock"):
            try:
                shutil.copy2(record, destination / record.name)
            except OSError:
                continue


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


@pytest.fixture(autouse=True)
def reap_watch_producers_armed_by_this_test(tmp_path, tmp_path_factory):
    """Reap, before the test's temporary homes are pruned, what it armed.

    A detached watch producer is found through the seat record under its
    configuration home. When temporary directories are kept only for failures,
    that home is removed at the test's own end — before the session-scoped
    reaper runs — so the record the reap depends on is already gone and the
    surviving producer is left to fail the session's liveness scan. Signalling
    here, at this test's teardown, keeps the record in place long enough to
    attribute the producer; requesting ``tmp_path`` makes this fixture finalize
    before ``tmp_path`` does. A producer that never wrote a record is not listed
    here and is still caught by the session-end scan.
    """
    yield
    root = tmp_path_factory.getbasetemp()
    reaped = sorted(set(reapable_watch_pids(root)))
    for pid in reaped:
        signal_worker(pid, signal.SIGTERM)
    await_exit(reaped)
    preserve_seat_records(root)


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


# A session given an explicit ``--basetemp`` removes and recreates that
# directory at startup, and this suite's reaper then signals every crew watch
# producer whose home lies under the same root. Two sessions sharing one
# basetemp therefore destroy each other's fixtures. The lock below admits one
# holder at a time, so a second session is refused before pytest removes
# anything. Sequential reuse is unaffected: an exited session releases the
# lock, and a lock whose pid is dead is taken over.
#
# The lock is a file beside the basetemp directory, never inside it, because
# the directory it guards is exactly what a session removes at startup.

_BASETEMP_LOCK_SUFFIX = ".lock"

# A lock present but not yet readable is a session mid-acquisition, not a
# stale lock to be taken over. The writer links a fully written file into
# place, so at rest the lock is always readable; the window still to guard is
# a lock another process created but has not finished writing, or one left by
# an interrupted write. Such a lock is refused while it is younger than this,
# and unlinked as stale once it is older.
_LOCK_PARTIAL_GRACE_SECONDS = 5.0


class BasetempInUseError(Exception):
    """A live session already holds the basetemp this one was given."""

    def __init__(self, basetemp: Path, holder_pid: int | None) -> None:
        self.basetemp = basetemp
        self.holder_pid = holder_pid
        if holder_pid is None:
            detail = (
                "--basetemp: a lock is being written by a session that has not "
                "yet named itself"
            )
        else:
            detail = f"--basetemp {basetemp} is held by a live pytest session (pid {holder_pid})"
        super().__init__(
            f"{detail}. A second session on the same basetemp removes "
            "and recreates it at startup, destroying the running session's "
            "fixtures; give this session its own basetemp."
        )


def basetemp_lock_path(basetemp: Path) -> Path:
    """The lock guarding ``basetemp``: a sibling file, never inside it."""
    return basetemp.parent / (basetemp.name + _BASETEMP_LOCK_SUFFIX)


def _lock_record(lock: Path) -> dict | None:
    """The lock's decoded holder record, or ``None`` if it is not readable yet."""
    try:
        value = json.loads(lock.read_text() or "{}")
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lock_holder_pid(lock: Path) -> int | None:
    record = _lock_record(lock)
    if record is None:
        return None
    pid = record.get("pid")
    return pid if isinstance(pid, int) and pid > 0 else None


def _lock_holder(lock: Path) -> tuple[int, int | None] | None:
    """The holder's pid and recorded start time, or ``None`` when unreadable."""
    record = _lock_record(lock)
    if record is None:
        return None
    pid = record.get("pid")
    if not (isinstance(pid, int) and pid > 0):
        return None
    start = record.get("start")
    return (pid, start if isinstance(start, int) else None)


def _process_start_time(pid: int) -> int | None:
    """Field 22 of ``/proc/<pid>/stat``: the process's start time in clock ticks.

    The executable name (field 2) may contain spaces and parentheses, so the
    fields after it are read from after the last ``)``. The returned value is
    fixed for the life of the process, so it distinguishes a live holder from
    an unrelated process that has since reused its pid. ``None`` when the file
    cannot be read (the process is gone, or is not ours).
    """
    try:
        stat = Path("/proc", str(pid), "stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tail = stat[stat.rindex(")") + 2 :]
    except ValueError:
        return None
    fields = tail.split()
    index = 22 - 3  # ``tail`` begins at field 3 (state); starttime is field 22.
    if len(fields) <= index:
        return None
    try:
        return int(fields[index])
    except ValueError:
        return None


def _process_is_alive(pid: int) -> bool:
    """Whether ``pid`` names a process that has not exited.

    ``os.kill(pid, 0)`` is the check: it raises ``ProcessLookupError`` for a
    pid the kernel holds no process for, ``PermissionError`` for a live process
    that is not ours, and returns otherwise. The last two both mean the pid is
    live, which is what the lock cares about.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _holder_is_live(holder: tuple[int, int | None]) -> bool:
    """Whether the lock's holder is the same still-running process that took it.

    A pid alone is not the identity: the kernel reuses pids, so a lock left by
    a killed session whose number an unrelated process now carries would refuse
    its basetemp forever. The holder records its start time beside its pid, and
    a live pid whose start time no longer matches is a different process — the
    lock is stale and is taken over. When either side has no start time to
    compare (a lock written before this field existed, or a ``/proc`` read that
    is not permitted), the pid being alive is the only evidence there is.
    """
    pid, recorded = holder
    if not _process_is_alive(pid):
        return False
    if recorded is None:
        return True
    current = _process_start_time(pid)
    if current is None:
        return True
    return recorded == current


def _lock_is_young(lock: Path) -> bool:
    """Whether ``lock`` was written less than ``_LOCK_PARTIAL_GRACE_SECONDS`` ago."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age < _LOCK_PARTIAL_GRACE_SECONDS


def _link_lock(lock: Path, basetemp: Path) -> None:
    """Create ``lock`` atomically, fully written, or fail if it already exists.

    The holder's record is written to a temporary sibling and linked into place
    with ``os.link``, which is atomic and refuses an existing target. The lock
    path therefore never exists without the holder's pid and start time: a
    reader that opens it mid-acquisition cannot see a half-written record and
    mistake it for a lock it may take.
    """
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "start": _process_start_time(os.getpid()),
            "basetemp": str(basetemp),
        }
    )
    staging = lock.with_name(f"{lock.name}.{os.getpid()}.tmp")
    try:
        with open(staging, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.link(staging, lock)
    finally:
        with contextlib.suppress(FileNotFoundError):
            staging.unlink()


def acquire_basetemp_lock(basetemp: Path) -> Path:
    """Take the lock beside ``basetemp``, or refuse when a live session holds it.

    ``os.link`` of a fully written sibling is the atomic step that decides
    ownership. A lock already present is refused when its holder is still the
    process that took it, or when it is too young to read and may be mid-write;
    a stale lock — its pid dead, or its pid reused by a different process — is
    removed and the acquisition retried, so a session that crashed without
    releasing is taken over rather than blocking reuse.
    """
    lock = basetemp_lock_path(basetemp)
    while True:
        try:
            _link_lock(lock, basetemp)
            return lock
        except FileExistsError:
            holder = _lock_holder(lock)
            if holder is not None and _holder_is_live(holder):
                raise BasetempInUseError(basetemp, holder[0]) from None
            if holder is None and _lock_is_young(lock):
                raise BasetempInUseError(basetemp, None) from None
            with contextlib.suppress(FileNotFoundError):
                lock.unlink()
            continue


def release_basetemp_lock(lock: Path) -> None:
    """Drop ``lock`` if this process still holds it."""
    if _lock_holder_pid(lock) != os.getpid():
        return
    with contextlib.suppress(FileNotFoundError):
        lock.unlink()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{ARMING_MARKER}: the test owns and reaps a real watch producer",
    )
    basetemp = getattr(config.option, "basetemp", None)
    if not basetemp:
        return
    resolved = Path(os.path.abspath(basetemp))
    try:
        lock = acquire_basetemp_lock(resolved)
    except BasetempInUseError as refused:
        raise pytest.UsageError(str(refused)) from None
    config.add_cleanup(lambda: release_basetemp_lock(lock))


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


@pytest.fixture(scope="session", autouse=True)
def _refuse_real_scheduler(tmp_path_factory):
    """Put a refusing stub for every scheduler verb first on PATH for the session.

    A test that provides its own recording scheduler prepends its own directory
    later, so its stub takes precedence and this one is never reached; a test
    that reaches a scheduler verb without providing one hits this stub instead,
    which records the reach and exits non-zero rather than letting the real
    client run.
    """
    directory = tmp_path_factory.mktemp("scheduler-shim")
    for verb in _SCHEDULER_VERBS:
        stub = directory / verb
        stub.write_text(_REFUSING_SCHEDULER_STUB, encoding="utf-8")
        stub.chmod(0o755)
    log = os.environ.get(_SCHEDULER_REACH_LOG_ENV)
    if not log:
        log = str(directory / "reached.tsv")
    os.environ[_SCHEDULER_REACH_LOG_ENV] = log
    previous = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([str(directory), previous])
    try:
        yield Path(log)
    finally:
        os.environ["PATH"] = previous


def reached_allocation_verbs(nodeid: str, log_text: str) -> list[str]:
    """The refused allocation verbs a test reached, each rendered as 'verb argv'.

    The guard stub appends one ``<test id>\\t<verb argv>`` line per refusal.
    Only the allocation verbs are returned: a read-only probe is recorded but
    does not fail the test that made it, so the guard refuses an allocation
    reach without turning an unrelated liveness probe red.
    """
    prefix = f"{nodeid} "
    reached: list[str] = []
    for line in log_text.split("\n"):
        if not line.startswith(prefix):
            continue
        verb_argv = line.split("\t", 1)[-1]
        if verb_argv.split(" ", 1)[0] in _ALLOCATION_VERBS:
            reached.append(verb_argv)
    return reached


@pytest.fixture(autouse=True)
def _fails_on_a_reached_scheduler(request, _refuse_real_scheduler):
    """Fail the test that reached an allocation verb it did not provide.

    The guard stub records every reach as it refuses; this reads back the
    allocation reaches attributed to this test and names the verb and its argv,
    so a forgotten recording scheduler surfaces as the assertion that names it
    rather than as a downstream failure. A read-only probe the test made is
    recorded but does not fail it.
    """
    yield
    log = _refuse_real_scheduler
    if not log.exists():
        return
    reached = reached_allocation_verbs(
        request.node.nodeid, log.read_text(encoding="utf-8")
    )
    if reached:
        raise AssertionError(
            "this test reached a scheduler verb it did not provide: "
            + "; ".join(reached)
            + " — provide a recording scheduler on PATH, or place into a held "
            "reservation, so no real scheduler client is run."
        )
