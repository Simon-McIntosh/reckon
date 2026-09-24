"""The served entry point binds and answers before the change watch is armed.

Arming a tree's change watch walks it, and a walk of a shared-filesystem tree
can take seconds. The served process must answer on its port throughout that
walk, so the fleet watch arms one tree at a time on its own thread while the
port serves. These cases measure the port's answer time against the arming
time by patching watch construction to take a fixed segment per tree.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from reckon import serve

_MOUNT_COUNT = 3
_ARM_SECONDS_PER_TREE = 5.0
_ANSWER_WITHIN_S = 2.0
_ARMED_WITHIN_S = 20.0
_POLL_S = 0.05


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mounted_projects(root: Path, count: int) -> dict[str, Path]:
    """Create ``count`` mounted project trees, each with one plan file."""

    mounts: dict[str, Path] = {}
    for index in range(count):
        name = f"project{index}"
        docs = root / name / "docs"
        (docs / "plans").mkdir(parents=True)
        (docs / "plans" / "example.html").write_text(
            '<!doctype html><html lang="en"><head>'
            '<meta charset="utf-8">'
            '<meta name="plan-slug" content="example">'
            '<meta name="plan-title" content="Example">'
            "</head><body></body></html>",
            encoding="utf-8",
        )
        mounts[name] = docs
    return mounts


def _await_answer(port: int, launched: float, timeout: float) -> int | None:
    """Return the status code answering mounts.json, or None before ``timeout``."""

    url = f"http://127.0.0.1:{port}/_projects/mounts.json"
    deadline = launched + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                return int(response.status)
        except (urllib.error.URLError, OSError):
            time.sleep(_POLL_S)
    return None


class _ServedEntryPoint:
    """A served process launched on its own thread over temporary mounts."""

    def __init__(self, harness: dict) -> None:
        self.port: int = harness["port"]
        self.thread: threading.Thread = harness["thread"]
        self.armed: list[Path] = harness["armed"]
        self.mounts: dict[str, Path] = harness["mounts"]
        self._servers: list = harness["servers"]

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        watch = serve._FLEET_WATCH
        if watch is not None:
            watch.close()


@pytest.fixture()
def served_entry_point(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Launch the served entry point with a slow (patched) watch construction.

    ``_ProjectChangeWatch`` construction is patched to take
    ``_ARM_SECONDS_PER_TREE`` per tree and to record every root it arms, so a
    process that binds only after arming every tree is measured as such.
    """

    config_home = tmp_path / "config"
    config_home.mkdir()
    mounts = _mounted_projects(tmp_path, _MOUNT_COUNT)
    mounts_file = config_home / "mounts.json"
    mounts_file.write_text(
        json.dumps({name: str(path) for name, path in mounts.items()}),
        encoding="utf-8",
    )

    monkeypatch.setenv("RECKON_HOME", str(config_home))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(config_home / "state"))
    # main() writes these module globals; restore them on teardown rather than
    # letting a served thread's configuration leak into sibling tests.
    monkeypatch.setattr(serve, "_MOUNTS_FILE", None)
    monkeypatch.setattr(serve, "_STATE_ROOT", None)
    monkeypatch.setattr(serve, "_SIGNATURE_TTL_S", 0.0)
    monkeypatch.setattr(serve, "_FLEET_WATCH", None)
    monkeypatch.setattr(serve.Handler, "_host", getattr(serve.Handler, "_host", None))
    monkeypatch.setattr(serve.Handler, "_port", getattr(serve.Handler, "_port", 0))

    armed: list[Path] = []
    real_watch = serve._ProjectChangeWatch

    def slow_watch(tree: Path):
        time.sleep(_ARM_SECONDS_PER_TREE)
        watch = real_watch(tree)
        armed.append(watch.root)
        return watch

    monkeypatch.setattr(serve, "_ProjectChangeWatch", slow_watch)

    servers: list = []

    class RecordingServer(serve.ThreadingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            servers.append(self)

    monkeypatch.setattr(serve, "ThreadingHTTPServer", RecordingServer)

    port = _free_port()
    thread = threading.Thread(
        target=serve.main,
        kwargs={"port": port, "host": "127.0.0.1", "mounts_file": mounts_file},
        daemon=True,
        name="served-entry-point",
    )
    harness = {
        "port": port,
        "thread": thread,
        "armed": armed,
        "mounts": mounts,
        "servers": servers,
    }
    entry_point = _ServedEntryPoint(harness)
    try:
        yield entry_point
    finally:
        entry_point.stop()


def test_mounts_answer_before_the_watch_is_armed(served_entry_point) -> None:
    entry_point = served_entry_point
    launched = time.monotonic()
    entry_point.thread.start()

    status = _await_answer(entry_point.port, launched, _ANSWER_WITHIN_S)
    assert status == 200, (
        "mounts.json did not answer 200 within 2 s of launch; the port is "
        "bound only after the change watch is built"
    )

    # Arming continues behind the answered port: the first tree takes the
    # patched 5 s, so an answer inside 2 s rules out an entry point that armed
    # every tree before serving.
    assert len(entry_point.armed) < _MOUNT_COUNT, (
        "every tree was already armed when the port answered, so the answer "
        "time did not measure arming on a background thread"
    )


def test_every_tree_is_armed_within_the_bound(served_entry_point) -> None:
    entry_point = served_entry_point
    launched = time.monotonic()
    entry_point.thread.start()

    deadline = launched + _ARMED_WITHIN_S
    while time.monotonic() < deadline and len(entry_point.armed) < _MOUNT_COUNT:
        time.sleep(_POLL_S)

    armed = {path.resolve() for path in entry_point.armed}
    expected = {path.resolve() for path in entry_point.mounts.values()}
    assert armed == expected, (
        f"only {len(armed)} of {_MOUNT_COUNT} trees were armed within 20 s"
    )


def test_the_entry_point_answers_while_a_watch_still_arms(served_entry_point) -> None:
    """A tree is covered by the reuse window until its watch is armed.

    The port answers mounts.json while the fleet watch has armed fewer than
    every tree, which is the state the reuse window has to cover.
    """

    entry_point = served_entry_point
    launched = time.monotonic()
    entry_point.thread.start()

    observed_unarmed = False
    deadline = launched + _ARMED_WITHIN_S
    while time.monotonic() < deadline:
        if (
            _await_answer(entry_point.port, time.monotonic(), 0.5) == 200
            and len(entry_point.armed) < _MOUNT_COUNT
        ):
            observed_unarmed = True
            break
        time.sleep(_POLL_S)

    assert observed_unarmed, (
        "the port never answered while a tree was still unarmed; the reuse "
        "window had no uncovered tree to cover"
    )


if __name__ == "__main__":  # pragma: no cover - manual launch
    raise SystemExit(pytest.main([os.path.abspath(__file__)]))
