"""The process-local lock registry does not grow with the paths ever locked.

A station or admit service takes ``exclusive_lock`` once per run it touches, so
a registry that kept every path would hold one lock per run ever seen. Each
entry must exist only while some thread holds or waits on that path, and two
threads contending on one held path must still exclude each other.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from reckon import node_exclusive_lock


def _registry() -> dict[str, threading.Lock]:
    return node_exclusive_lock._PROCESS_LOCKS


def test_no_entry_is_kept_for_a_path_no_thread_holds(tmp_path: Path) -> None:
    count = 2000
    for index in range(count):
        with node_exclusive_lock.exclusive_lock(tmp_path / f"run-{index}.lock"):
            pass

    held = dict(_registry())
    assert not held, (
        f"the registry kept {len(held)} of {count} entries for paths no thread "
        f"holds: {sorted(held)[:5]}"
    )


def test_two_threads_contending_on_one_path_still_exclude(tmp_path: Path) -> None:
    path = tmp_path / "contended.lock"
    entered: list[str] = []
    first_holds = threading.Event()
    release_first = threading.Event()

    def hold(
        name: str, holds: threading.Event | None, release: threading.Event | None
    ) -> None:
        with node_exclusive_lock.exclusive_lock(path):
            if holds is not None:
                holds.set()
            if release is not None:
                release.wait(timeout=30)
            entered.append(name)
            time.sleep(0.05)

    first = threading.Thread(target=hold, args=("first", first_holds, release_first))
    first.start()
    try:
        assert first_holds.wait(timeout=30), "the first thread never took the lock"
        second = threading.Thread(target=hold, args=("second", None, None))
        second.start()
        try:
            _wait_for(
                lambda: node_exclusive_lock._PROCESS_LOCKS_USERS.get(str(path), 0) >= 2
            )
            assert entered == [], (
                f"the second thread entered while the first held: {entered}"
            )
        finally:
            release_first.set()
    finally:
        release_first.set()
    first.join(timeout=60)
    second.join(timeout=60)

    assert entered == ["first", "second"], f"the order was {entered}"
    assert str(path) not in _registry(), "the released path is still in the registry"


def _wait_for(condition: Callable[[], bool], seconds: float = 30) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("the condition never held")
