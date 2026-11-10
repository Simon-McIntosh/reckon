"""A session host never waits in a read it cannot leave, and never runs beside one.

The request descriptor is a FIFO, and a FIFO can have more than one reader. When
select reports it readable, another reader sharing it can take the bytes before
the host gets to them, and a blocking read then waits for input that has already
gone: the host stops reading requests and never sees a stop. The host therefore
reads the descriptor non-blocking, and a pass whose bytes went elsewhere falls
through to the owner check and the tick.

The other three properties here protect the same host's place. One host serves
one owner pair: it holds an advisory lock named for the pair, and a second host
started for a pair whose lock is held exits stating why and leaves the first
host's FIFO in place. And the FIFO a host removes at stop is the FIFO it was
handed, decided by device and inode rather than by path, so a path that now names
a later session's FIFO is left alone.

Each absence is shown beside the same instrument reading a known-present value:
the reader that takes the bytes is shown to have taken them, and the FIFO a host
may remove is shown removed when its identity still matches.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from reckon.crew import session_host as module

# A poll cadence short enough that two intervals fit inside a case, and long
# enough that a busy node does not turn an idle wait into a false failure.
POLL_SECONDS = 0.1
PER_TEST_TIMEOUT_SECONDS = 8

pytestmark = pytest.mark.timeout(PER_TEST_TIMEOUT_SECONDS)


def _environ(tmp_path: Path) -> dict[str, str]:
    environ = dict(os.environ)
    environ["RECKON_SESSION_HOST_STATE_DIR"] = str(tmp_path / "state")
    environ["RECKON_SESSION_HOST_LOG_DIR"] = str(tmp_path / "logs")
    environ["RECKON_SESSION_HOST_POLL_SECONDS"] = str(POLL_SECONDS)
    environ["RECKON_SESSION_HOST_BACKOFF"] = str(POLL_SECONDS)
    return environ


def _owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tag: str | None = None
) -> dict:
    """One owner pair and the FIFO path the host resolves for it.

    The pid is this process's own and the start tick is that process's real one,
    so the owner is genuinely live and the supervision tick runs. A ``tag``
    suffixes the tick so a case can name a second pair -- and a second FIFO --
    without pretending to be another process; a pair given a tag is used only
    where the owner is never checked.
    """
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    start = module.process_start_time(os.getpid()) or "0"
    if tag is not None:
        start = f"{start}-{tag}"
    return {"pid": os.getpid(), "start_time": start}


def _make_fifo(owner: dict) -> Path:
    path = module._fifo_path(owner)
    assert path is not None, "the fixture pair resolved no FIFO path"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    os.mkfifo(path)
    return path


class _TickCountingHost(module.SessionHost):
    """A host that counts its ticks, so a pass can be shown to have finished."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.ticks = 0
        self.stop_after_ticks: int | None = None

    def tick(self) -> None:
        self.ticks += 1
        super().tick()
        if self.stop_after_ticks is not None and self.ticks >= self.stop_after_ticks:
            self.request_stop()


def _stealing_select(original, other_reader: int, stolen: list[bytes]):
    """A select that reports the reader readable after another reader drains it.

    The first call does exactly what the race does: bytes are in the FIFO, select
    reports it readable, and the other reader -- not the host -- takes them. The
    host is then handed a readable descriptor with nothing left in it.
    """
    calls = {"count": 0}

    def _select(rlist, wlist, xlist, timeout=None):
        calls["count"] += 1
        if calls["count"] == 1:
            stolen.append(os.read(other_reader, 65536))
            # Only the request descriptor: the wake pipe is idle here, and
            # reporting it readable would end the concern, and the case, early.
            return ([rlist[0]], [], [])
        return original(rlist, wlist, xlist, timeout)

    return _select


def test_a_read_another_reader_emptied_ends_the_pass_and_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) A shared FIFO whose bytes another reader took does not strand the host.

    Both readers are real: the host holds one read-write descriptor on the FIFO
    and a second reader holds another. The bytes are written once, the other
    reader takes them after select reports the FIFO readable, and the host's read
    finds nothing. The pass must then finish -- one tick -- well inside two poll
    intervals, and the request must be shown to have gone to the other reader
    rather than being silently dropped by the host.
    """
    owner = _owner(tmp_path, monkeypatch)
    fifo = _make_fifo(owner)
    environ = _environ(tmp_path)

    host = _TickCountingHost(
        owner=owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    host.stop_after_ticks = 1

    writer = os.open(fifo, os.O_RDWR)
    other_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    requests = os.fdopen(os.open(fifo, os.O_RDWR), "r", errors="replace")
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    stolen: list[bytes] = []
    request = b'{"project": "alpha", "session": "sess"}\n'
    os.write(writer, request)
    monkeypatch.setattr(
        module.select,
        "select",
        _stealing_select(module.select.select, other_reader, stolen),
    )
    try:
        started = time.monotonic()
        host.supervise(requests, wake_read)
        elapsed = time.monotonic() - started
    finally:
        os.close(writer)
        os.close(other_reader)
        requests.close()
        os.close(wake_read)
        os.close(wake_write)

    assert stolen and stolen[0] == request, (
        f"the other reader did not take the request bytes: {stolen!r}"
    )
    assert host.ticks == 1, f"the pass did not reach exactly one tick: {host.ticks}"
    assert elapsed < 2 * POLL_SECONDS, (
        f"the pass took {elapsed:.3f}s, beyond two {POLL_SECONDS}s poll intervals"
    )
    record = host.record()
    assert record["children"] == [], (
        f"the host handled bytes another reader took: {record['children']!r}"
    )


def test_a_stop_wakes_a_host_that_is_waiting_on_a_drained_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A stop request and its wake-pipe write end the host in that state.

    The host is put in the same state as (a) -- select reported the FIFO readable
    and another reader took the bytes -- and a stop request is then made from
    outside with the wake write a signal handler makes. The supervise must return
    within two poll intervals of that write.
    """
    owner = _owner(tmp_path, monkeypatch)
    fifo = _make_fifo(owner)
    environ = _environ(tmp_path)

    host = module.SessionHost(
        owner=owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    writer = os.open(fifo, os.O_RDWR)
    other_reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    requests = os.fdopen(os.open(fifo, os.O_RDWR), "r", errors="replace")
    wake_read, wake_write = os.pipe()
    os.set_blocking(wake_read, False)
    os.set_blocking(wake_write, False)
    stolen: list[bytes] = []
    os.write(writer, b'{"project": "alpha", "session": "sess"}\n')
    monkeypatch.setattr(
        module.select,
        "select",
        _stealing_select(module.select.select, other_reader, stolen),
    )
    finished: list[float] = []

    def _supervise() -> None:
        host.supervise(requests, wake_read)
        finished.append(time.monotonic())

    worker = threading.Thread(target=_supervise, daemon=True)
    worker.start()
    # Let the host reach the pass whose read finds nothing, then stop it the way
    # a signal handler does: set the request and write the wake pipe.
    time.sleep(3 * POLL_SECONDS)
    stopped_at = time.monotonic()
    host.request_stop()
    os.write(wake_write, b"x")
    worker.join(timeout=4 * POLL_SECONDS)
    try:
        assert not worker.is_alive(), (
            "the host did not return after a stop request and its wake write"
        )
        assert finished, "the supervise returned without recording its finish"
        elapsed = finished[0] - stopped_at
        assert elapsed < 2 * POLL_SECONDS, (
            f"the host took {elapsed:.3f}s to end, beyond two poll intervals"
        )
        assert stolen and stolen[0], "the other reader did not take the request bytes"
    finally:
        os.close(writer)
        os.close(other_reader)
        requests.close()
        os.close(wake_read)
        os.close(wake_write)


def test_a_second_host_for_a_held_pair_exits_stating_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """(c) A second host for an owner pair whose lock is held leaves the first alone.

    The first host takes the pair's lock. The second host is started through the
    entry point against the same pair and the same FIFO; it must exit with the
    already-running status, say on stderr why it did, keep its hands off the
    first host's census, and leave the FIFO in place.
    """
    owner = _owner(tmp_path, monkeypatch, "held-pair")
    fifo = _make_fifo(owner)
    environ = _environ(tmp_path)

    first = module.SessionHost(
        owner=owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    assert first.acquire_owner_lock(), "the first host could not take the pair's lock"
    identity = module._fifo_identity(fifo)
    assert identity is not None, "the fixture FIFO is not there to be identified"
    second = module.SessionHost(
        owner=owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    requests = os.fdopen(os.open(fifo, os.O_RDWR), "r", errors="replace")
    try:
        code = module.run(
            owner=owner,
            follower_argv=["/bin/true"],
            environ=environ,
            requests=requests,
        )
    finally:
        requests.close()
        first.release_owner_lock()

    stderr = capsys.readouterr().err
    assert code == module.ALREADY_RUNNING_EXIT_CODE, f"the second host returned {code}"
    assert module.ALREADY_RUNNING_REASON in stderr, (
        f"the second host did not state why it exited: {stderr!r}"
    )
    assert fifo.exists(), "the second host removed the first host's FIFO"
    assert module._fifo_identity(fifo) == identity, "the FIFO was replaced at stop"
    assert not second.record_path.exists(), "the second host wrote a census record"


def test_a_fifo_that_now_names_another_file_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) Stop removes the FIFO this host was handed, and no other at that path.

    The host records the identity of its FIFO. That FIFO is then replaced at the
    same path, the way a later session's FIFO would be, and the host is stopped:
    the new file must survive. A second host whose own FIFO is still the one it
    was handed is stopped beside it in the same case, so the removal instrument
    is shown to work rather than merely to be quiet.
    """
    replaced_owner = _owner(tmp_path, monkeypatch, "replaced-fifo")
    replaced = _make_fifo(replaced_owner)
    environ = _environ(tmp_path)

    host = module.SessionHost(
        owner=replaced_owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    assert host._fifo_identity is not None, "the host recorded no FIFO identity"
    replaced.unlink()
    os.mkfifo(replaced)
    later_identity = module._fifo_identity(replaced)
    assert later_identity != host._fifo_identity, (
        "the replacement FIFO reused the removed file's identity"
    )

    host.stop("signalled")

    assert replaced.exists(), "the host removed a FIFO that was not the one it held"

    kept_owner = _owner(tmp_path, monkeypatch, "kept-fifo")
    kept = _make_fifo(kept_owner)
    control = module.SessionHost(
        owner=kept_owner,
        follower_argv=["/bin/true"],
        environ=environ,
        live_view=lambda _project, _session: None,
    )
    assert control._fifo_identity is not None, "the control host recorded no identity"
    control.stop("signalled")
    assert not kept.exists(), "the control host left the FIFO it was handed behind"
