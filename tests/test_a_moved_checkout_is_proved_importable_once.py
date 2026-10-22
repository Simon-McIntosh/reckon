"""A published image is proved once, and every asker reads that one verdict.

Every follower on a node checks its checkout on the same cadence and reloads
onto the same published image at the same moment, so a herd of them used to run
the same thirty-second import proof together. Whether an image imports is a fact
about the image and the interpreter, not about the process asking, so the first
asker proves it once under an exclusive lock in the image's own directory and
records the verdict beside it, keyed by the interpreter. An asker that finds the
proof lock held is told the proof is pending at once and reads the verdict at
its next check, so a slow proof never stalls a follower that is not running it.
A source checkout has no immutable tree and keeps the per-process proof. A retry
earned by a timeout is spread across the nodes' followers by a jitter, so a herd
does not stay in step.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
from importlib import import_module
from pathlib import Path

import pytest

from reckon import checkout_trust
from reckon import crew_follow_commands as follow
from reckon.crew import follower_reload, runs

TREE_ID = "a" * 40
OTHER_TREE_ID = "b" * 40


def _image(root: Path, name: str = TREE_ID) -> Path:
    """A published image: named by a package tree id, carrying its marker."""
    image = root / name
    (image / "reckon").mkdir(parents=True)
    (image / "reckon" / "__init__.py").write_text("")
    (image / ".complete").touch()
    return image


class _Probe:
    """A stand-in import probe that counts its runs and can be made to answer."""

    def __init__(self) -> None:
        self.calls: list = []
        self.answer = lambda: subprocess.CompletedProcess([], 0, "", "")
        self.delay = 0.0
        self.lock = threading.Lock()

    def run(self, *args, **kwargs):
        with self.lock:
            self.calls.append(args)
        if self.delay:
            time.sleep(self.delay)
        return self.answer()


@pytest.fixture()
def probe(monkeypatch) -> _Probe:
    stub = _Probe()
    monkeypatch.setattr(checkout_trust.subprocess, "run", stub.run)
    return stub


def _answer_loads():
    return subprocess.CompletedProcess([], 0, "", "")


def _answer_fails():
    return subprocess.CompletedProcess(
        [], 1, "", "ModuleNotFoundError: No module named 'reckon.a_module_that_is_gone'"
    )


def _answer_times_out():
    raise subprocess.TimeoutExpired("probe", checkout_trust.IMPORT_PROBE_TIMEOUT)


def test_concurrent_askers_on_one_image_start_one_probe(tmp_path, probe):
    image = _image(tmp_path)
    probe.delay = 0.05
    askers = 16
    start = threading.Barrier(askers)
    results: list = []

    def ask() -> None:
        start.wait()
        results.append(checkout_trust.reckon_imports(image))

    threads = [threading.Thread(target=ask) for _ in range(askers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(probe.calls) == 1, f"{len(probe.calls)} probes ran for one image"
    assert (True, "") in results, results
    assert all(
        result == (True, "") or isinstance(result[1], checkout_trust.ImportProofPending)
        for result in results
    ), results
    # Every asker told the proof was pending reads the one verdict next time.
    assert [checkout_trust.reckon_imports(image) for _ in range(askers)] == [
        (True, "")
    ] * askers
    assert len(probe.calls) == 1, "a later ask proved the image again"


def test_concurrent_askers_all_receive_a_recorded_failure(tmp_path, probe):
    image = _image(tmp_path)
    probe.answer = _answer_fails
    probe.delay = 0.05
    askers = 16
    start = threading.Barrier(askers)
    results: list = []

    def ask() -> None:
        start.wait()
        results.append(checkout_trust.reckon_imports(image))

    threads = [threading.Thread(target=ask) for _ in range(askers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(probe.calls) == 1, f"{len(probe.calls)} probes ran for one image"
    answered = [
        result
        for result in results
        if not isinstance(result[1], checkout_trust.ImportProofPending)
    ]
    assert answered, results
    assert all(not ok and "a_module_that_is_gone" in reason for ok, reason in answered)
    later = [checkout_trust.reckon_imports(image) for _ in range(askers)]
    assert all(result == answered[0] for result in later), later
    assert len(probe.calls) == 1, "a later ask proved the image again"


def test_a_timed_out_probe_records_nothing_and_the_next_asker_probes(tmp_path, probe):
    image = _image(tmp_path)
    probe.answer = _answer_times_out

    ok, reason = checkout_trust.reckon_imports(image)
    assert ok is False
    assert isinstance(reason, checkout_trust.ImportProbeTimedOut), reason
    # A timeout says nothing about the image, so no verdict was written.
    assert list(image.glob(f"{checkout_trust.IMPORT_VERDICT_PREFIX}*")) == []

    ok, reason = checkout_trust.reckon_imports(image)
    assert len(probe.calls) == 2, "the next asker did not probe a timed-out image"


def test_a_recorded_failure_is_returned_without_probing_again(tmp_path, probe):
    image = _image(tmp_path)
    probe.answer = _answer_fails

    first = checkout_trust.reckon_imports(image)
    second = checkout_trust.reckon_imports(image)

    assert len(probe.calls) == 1, "a recorded failure was probed again"
    assert first == second
    assert first[0] is False
    assert "a_module_that_is_gone" in first[1], first


def test_a_new_tree_id_is_probed_afresh(tmp_path, probe):
    first_image = _image(tmp_path, TREE_ID)
    second_image = _image(tmp_path, OTHER_TREE_ID)

    assert checkout_trust.reckon_imports(first_image) == (True, "")
    assert checkout_trust.reckon_imports(second_image) == (True, "")
    assert len(probe.calls) == 2, "a second image reused the first's verdict"


def test_a_verdict_for_another_interpreter_is_not_reused(tmp_path, probe, monkeypatch):
    image = _image(tmp_path)
    monkeypatch.setattr(checkout_trust.sys, "executable", "/usr/bin/python-one")
    assert checkout_trust.reckon_imports(image) == (True, "")

    monkeypatch.setattr(checkout_trust.sys, "executable", "/usr/bin/python-two")
    assert checkout_trust.reckon_imports(image) == (True, "")
    assert len(probe.calls) == 2, (
        "a verdict proven under another interpreter was reused"
    )


def test_a_source_checkout_keeps_its_per_process_probe(tmp_path, probe):
    source = tmp_path / "checkout"
    (source / "reckon").mkdir(parents=True)
    (source / "reckon" / "__init__.py").write_text("")

    assert checkout_trust.reckon_imports(source) == (True, "")
    assert checkout_trust.reckon_imports(source) == (True, "")
    assert len(probe.calls) == 2, "a source checkout shared a per-process probe"


def test_a_published_image_without_its_marker_is_not_shared(tmp_path, probe):
    bare = tmp_path / TREE_ID
    (bare / "reckon").mkdir(parents=True)

    assert checkout_trust.reckon_imports(bare) == (True, "")
    assert checkout_trust.reckon_imports(bare) == (True, "")
    assert len(probe.calls) == 2, "a tree carrying no completion marker was shared"


def _hold_probe_lock(image: Path) -> int:
    """Hold the image's probe lock, as an asker that is slow to prove would."""
    descriptor = os.open(
        image / checkout_trust.IMPORT_PROBE_LOCK, os.O_CREAT | os.O_RDWR, 0o600
    )
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def test_a_held_lock_reports_a_pending_proof_at_once(tmp_path, probe):
    image = _image(tmp_path)
    holder = _hold_probe_lock(image)
    try:
        started = time.monotonic()
        imports, reason = checkout_trust.reckon_imports(image)
        waited = time.monotonic() - started
    finally:
        os.close(holder)

    assert imports is False
    assert isinstance(reason, checkout_trust.ImportProofPending), reason
    assert not isinstance(reason, checkout_trust.ImportProbeTimedOut), reason
    assert probe.calls == [], "the asker ran a probe of its own on a held lock"
    assert waited < 1.0, f"the asker waited {waited:.2f} s on a held lock"


def test_the_holders_verdict_is_read_at_the_next_ask(tmp_path, probe):
    image = _image(tmp_path)
    holder = _hold_probe_lock(image)
    try:
        _, reason = checkout_trust.reckon_imports(image)
        assert isinstance(reason, checkout_trust.ImportProofPending), reason
        checkout_trust._record_import_verdict(
            image, imports=False, reason="ModuleNotFoundError: gone"
        )
        later = checkout_trust.reckon_imports(image)
    finally:
        os.close(holder)

    assert later == (False, "ModuleNotFoundError: gone"), later
    assert probe.calls == [], "the asker probed although the holder answered"


def test_a_held_pin_is_renewed_once_per_interval(tmp_path, monkeypatch):
    image = _image(tmp_path)
    clock = [5000.0]
    monkeypatch.setattr(checkout_trust.time, "monotonic", lambda: clock[0])
    stamps: list = []
    real_utime = os.utime

    def counting_utime(target, *args, **kwargs):
        stamps.append(target)
        return real_utime(target, *args, **kwargs)

    descriptor = checkout_trust.pin_materialized_tree(image)
    monkeypatch.setattr(checkout_trust.os, "utime", counting_utime)
    try:
        for _ in range(50):
            checkout_trust.refresh_materialized_tree_pin(descriptor, image)
            clock[0] += 0.1
        assert stamps == [], f"{len(stamps)} renewals inside one interval"

        clock[0] += checkout_trust.COMMITTED_TREE_LEASE_RENEW_SECONDS
        checkout_trust.refresh_materialized_tree_pin(descriptor, image)
        assert stamps == [descriptor], stamps
    finally:
        os.close(descriptor)


@pytest.fixture()
def reloader(monkeypatch):
    """A reloader holding a stale image at a moved code stamp, plus its clock."""
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: "moved")
    instance = follow._FollowerReloader("jitter-fixture", None)
    instance.code_stamp = "stale"
    monkeypatch.setattr(
        follow.checkout_trust,
        "tracked_reckon_tree_is_dirty",
        lambda root: (False, ""),
    )
    dispatch = import_module("reckon.crew.dispatch")
    monkeypatch.setattr(dispatch, "_export_launched_workers_for_reexec", lambda: None)
    clock = [1000.0]
    monkeypatch.setattr(follow.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(follow.os, "execve", lambda *args, **kwargs: None)
    return instance, clock


def _time_out(monkeypatch):
    monkeypatch.setattr(
        follow.checkout_trust.subprocess,
        "run",
        lambda *args, **kwargs: _answer_times_out(),
    )


def test_the_timeout_retry_wait_carries_jitter(reloader, monkeypatch):
    instance, clock = reloader
    _time_out(monkeypatch)
    monkeypatch.setattr(follower_reload.random, "uniform", lambda low, high: high * 0.5)

    instance.poll({})

    initial = follow.FOLLOWER_RELOAD_PROBE_RETRY_INITIAL_SECONDS
    fraction = follow.FOLLOWER_RELOAD_PROBE_RETRY_JITTER_FRACTION
    assert instance.probe_retry_jitter == pytest.approx(initial * fraction * 0.5), (
        instance.probe_retry_jitter
    )
    assert 0.0 < instance.probe_retry_jitter <= initial * fraction

    # A poll at the jittered instant probes again, though the nominal instant --
    # the longest a checkout is held -- has not yet passed.
    nominal = instance.probe_retry_at
    clock[0] = nominal - instance.probe_retry_jitter
    assert clock[0] < nominal
    instance.poll({})
    assert instance.probe_retry_at > nominal, (
        "the poll at the jittered instant did not probe"
    )


def test_the_jitter_spreads_two_followers_that_timed_out_together(
    reloader, monkeypatch
):
    instance, clock = reloader
    _time_out(monkeypatch)
    draws = iter([0.2, 0.9])

    def draw(low, high):
        return high * next(draws)

    monkeypatch.setattr(follower_reload.random, "uniform", draw)

    instance.poll({})
    first_gap = instance.probe_retry_gap
    first = first_gap - instance.probe_retry_jitter
    assert 0.0 < first < first_gap

    # The second timeout is taken at the first's nominal instant, and draws its
    # own fraction, so the two waits a herd timed out together do not coincide.
    clock[0] = instance.probe_retry_at
    instance.poll({})
    second = instance.probe_retry_gap - instance.probe_retry_jitter
    assert 0.0 < second < instance.probe_retry_gap


def _pending(monkeypatch):
    monkeypatch.setattr(
        follow.checkout_trust,
        "reckon_imports",
        lambda root: (
            False,
            follow.checkout_trust.ImportProofPending("another process is proving"),
        ),
    )


def test_a_pending_proof_is_rechecked_soon_and_silently(reloader, monkeypatch, capsys):
    instance, clock = reloader
    _pending(monkeypatch)
    initial_gap = instance.probe_retry_gap

    instance.poll({})

    assert capsys.readouterr().out == "", "a pending proof printed a line"
    assert instance.probe_retry_gap == initial_gap, "a pending proof doubled the gap"
    assert instance.timeout_deferral is False
    assert instance.deferred_stamp is None
    pending = follow.FOLLOWER_RELOAD_PROOF_PENDING_SECONDS
    assert instance.probe_retry_at == pytest.approx(clock[0] + pending)


def test_a_poll_inside_a_retry_wait_leaves_the_image_alone(
    reloader, monkeypatch, tmp_path
):
    instance, clock = reloader
    monkeypatch.setattr(runs, "follower_code_stamp", lambda: TREE_ID)
    instance.code_stamp = OTHER_TREE_ID
    materialised: list = []

    def materialise(head):
        materialised.append(head)
        return tmp_path, [], [], None

    monkeypatch.setattr(instance, "_materialize_replacement", materialise)
    _pending(monkeypatch)
    instance.poll({})
    assert materialised == [TREE_ID]

    clock[0] += runs.FOLLOWER_FRESHNESS_SECONDS
    assert clock[0] < instance.probe_retry_at - instance.probe_retry_jitter
    instance.poll({})

    assert materialised == [TREE_ID], "the image was re-materialised inside its wait"


def test_a_pending_proof_reloads_once_its_verdict_is_read(reloader, monkeypatch):
    instance, clock = reloader
    _pending(monkeypatch)
    instance.poll({})
    executed: list = []
    monkeypatch.setattr(follow.os, "execve", lambda *args, **kwargs: executed.append(1))
    monkeypatch.setattr(
        follow.checkout_trust, "reckon_imports", lambda root: (True, "")
    )

    clock[0] = instance.probe_retry_at
    instance.poll({})

    assert executed == [1], "the follower did not reload once the verdict arrived"
