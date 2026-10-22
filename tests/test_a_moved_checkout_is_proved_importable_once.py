"""A published image is proved once, and every asker reads that one verdict.

Every follower on a node checks its checkout on the same cadence and reloads
onto the same published image at the same moment, so a herd of them used to run
the same thirty-second import proof together. Whether an image imports is a fact
about the image and the interpreter, not about the process asking, so the first
asker proves it once under an exclusive lock in the image's own directory and
records the verdict beside it, keyed by the interpreter. A source checkout has
no immutable tree and keeps the per-process proof. A retry earned by a timeout
is spread across the nodes' followers by a jitter, so a herd does not stay in
step.
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
    assert results == [(True, "")] * askers


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
    assert all(not ok and reason == results[0][1] for ok, reason in results)
    assert "a_module_that_is_gone" in results[0][1], results[0]


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


def test_a_held_lock_makes_the_waiting_asker_time_out_without_probing(
    tmp_path, probe, monkeypatch
):
    image = _image(tmp_path)
    monkeypatch.setattr(checkout_trust, "IMPORT_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(checkout_trust, "IMPORT_PROBE_LOCK_POLL_SECONDS", 0.01)
    holder = _hold_probe_lock(image)
    try:
        started = time.monotonic()
        imports, reason = checkout_trust.reckon_imports(image)
        waited = time.monotonic() - started
    finally:
        os.close(holder)

    assert imports is False
    assert isinstance(reason, checkout_trust.ImportProbeTimedOut), reason
    assert "lock" in reason, reason
    assert probe.calls == [], "the waiter ran a probe of its own on a held lock"
    assert waited < 5.0, waited


def test_a_verdict_recorded_while_the_asker_waits_is_read(tmp_path, probe, monkeypatch):
    image = _image(tmp_path)
    monkeypatch.setattr(checkout_trust, "IMPORT_PROBE_LOCK_POLL_SECONDS", 0.01)
    holder = _hold_probe_lock(image)

    def prove_and_release():
        time.sleep(0.1)
        checkout_trust._record_import_verdict(
            image, imports=False, reason="ModuleNotFoundError: gone"
        )
        os.close(holder)

    writer = threading.Thread(target=prove_and_release)
    writer.start()
    try:
        imports, reason = checkout_trust.reckon_imports(image)
    finally:
        writer.join()

    assert (imports, reason) == (False, "ModuleNotFoundError: gone"), (imports, reason)
    assert probe.calls == [], "the waiter probed although a verdict arrived"


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
