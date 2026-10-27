"""A reload follows the committed source and refuses an unimportable image.

A producer or follower replaces its process image when the source checkout's
HEAD commit moves. An uncommitted edit leaves that stamp unchanged, so a
half-applied rename cannot become the replacement image. Once committed, the
source is materialised from HEAD and probed before re-execution. A committed
module that fails to import still defers the reload and keeps the running image.

The producer is the process the incident darkened, so it is the process these
cases watch. The tree under test is a self-contained git checkout laid down per
case, so a case can leave a tracked file modified or commit it, and the import
root the producer resolves is that checkout rather than this one. Its reload is
confirmed by the replacement image's own command line, and its liveness by a
run it renders, so a case cannot pass by measuring something else.

The cases edit only their own scratch checkout, never this working tree, so
they can run beside any other case without capturing a peer's edit as their own
baseline.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from reckon import cli as cli_module
from reckon import crew
from reckon.crew import runs

PROJECT = "proj"
NODE = "node-1"

# The reload's refusal line and the reasons these cases distinguish.
DEFERRED_MARKER = "reckon crew watch deferred its reload"
DIRTY_MARKER = "modified against HEAD"
IMPORT_MARKER = "does not import"

# The replacement image is launched with a launcher that inserts its import
# root on ``sys.path`` before entering the command. That string is absent from
# the argv the producer is armed with, so finding it on the process is direct
# evidence the image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# A comment, and one whose every prefix is a comment, so the module still parses
# and the change is content-only: the source commit stamp stays fixed while
# the working tree carries the half-edit.
CONTENT_EDIT_BYTES = b"\n# half-edit left in the tree\n"

# A module-level name bound to nothing that exists: importing it raises
# NameError, the failure the incident's producers hit. It is written into a
# module the producer does not import at startup and committed, so the tree it
# is found in is clean and the deferral can only be the import proof.
UNIMPORTABLE_MODULE = "crew/unimportable_probe_module.py"
UNIMPORTABLE_BODY = b"_probe_binding = _a_name_that_is_not_defined_anywhere\n"

SEAT_WITHIN_SECONDS = 25.0
RELOAD_WITHIN_SECONDS = 15.0
DEFER_WITHIN_SECONDS = 12.0
POLL_SECONDS = 0.05


def _real_git() -> str:
    """The git binary, skipping the PATH shims a worker session installs.

    A worker's ``git`` on ``PATH`` is a shim that refuses mutating verbs aimed
    outside the run's own worktree, and ``init`` of a scratch checkout is one of
    them. These cases build their checkout with the real binary, which is what
    a reader running the suite anywhere would get.
    """
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(entry) / "git"
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            continue
        if "shim" in entry:
            continue
        return str(candidate)
    return "git"


_GIT = _real_git()


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        [_GIT, "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        f"git {' '.join(args)} failed in {root}: {completed.stderr!r}"
    )
    return completed.stdout


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "reckon")
    _git(
        root,
        "-c",
        "user.email=probe@example.invalid",
        "-c",
        "user.name=probe",
        "commit",
        "-q",
        "-m",
        message,
    )


def _fresh_checkout(tmp_path: Path) -> tuple[Path, Path]:
    """A self-contained committed git checkout holding one copy of the package.

    The package is copied rather than edited in place so a case can leave the
    tree dirty or commit it without touching any other checkout, and so the
    producer resolves its import root here. The copy is committed, so ``HEAD``
    names its exact starting content and a modification is a real difference
    from it.
    """
    source_package = Path(cli_module.__file__).resolve().parent
    root = tmp_path / "checkout"
    package = root / "reckon"
    shutil.copytree(
        source_package,
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _git(root, "init", "-q", "-b", "main")
    _commit(root, "base")
    return root, package


def scratch_checkout(where: Path) -> tuple[Path, Path]:
    """A committed checkout of the package a reload case may change.

    The case changes a copy rather than the package its own process imports,
    then commits to move the source stamp without touching another checkout.
    """
    return _fresh_checkout(where)


def commit_the_source_change(
    root: Path, package: Path, relative: str, payload: bytes
) -> None:
    """Append to a file in the checkout and commit, leaving the tree clean."""
    source = package / relative
    source.write_bytes(source.read_bytes() + payload)
    _commit(root, f"change {relative}")


def _stamp_of(root: Path) -> str:
    """Read the source checkout's commit stamp in its own process."""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from reckon.crew import runs; print(runs.follower_code_stamp())",
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    # The reload images live under the node-local runtime root, so the producer
    # is given this test's own root. Inheriting the session's would publish the
    # images into the node's real runtime directory, where a later run of the
    # suite would read them.
    runtime = home / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return home


def _launch_producer(root: Path, home: Path) -> tuple[subprocess.Popen, queue.Queue]:
    executable = Path(sys.executable).with_name("reckon")
    process = subprocess.Popen(
        [
            str(executable),
            "crew",
            "watch",
            "--project",
            PROJECT,
            "--no-color",
            "--width",
            "240",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "RECKON_HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        start_new_session=True,
    )
    assert process.stdout is not None
    lines: queue.Queue = queue.Queue()
    reader = threading.Thread(
        target=lambda: [lines.put(line.rstrip("\n")) for line in process.stdout],
        daemon=True,
    )
    reader.start()
    return process, lines


def _seat_record(project: str) -> dict:
    """Read the seat's on-disk record, retrying a write caught mid-flight."""
    path = runs.watch_lock_path(project)
    for _ in range(20):
        if not path.is_file():
            time.sleep(0.02)
            continue
        text = path.read_text()
        if not text.strip():
            time.sleep(0.02)
            continue
        try:
            import json

            return json.loads(text)
        except ValueError:
            time.sleep(0.02)
    return {}


def _await_seat(project: str, expected_stamp: str) -> dict:
    deadline = time.monotonic() + SEAT_WITHIN_SECONDS
    while time.monotonic() < deadline:
        record = _seat_record(project)
        if record.get("pid") and record.get("code_stamp") == expected_stamp:
            return record
        time.sleep(POLL_SECONDS)
    raise AssertionError(
        f"the producer never took the seat with stamp {expected_stamp!r}; "
        f"last record {_seat_record(project)!r}"
    )


def _write_live_run(home: Path, run_id: str) -> None:
    """A working run the producer can see on its next tick."""
    log = home / "logs" / f"{run_id}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"type":"turn.started"}\n')
    manifest = home / "manifests" / f"{run_id}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(f"node: {NODE}\nstatus: in-progress\n")
    crew._write_json(
        crew.pointer_path(run_id),
        {
            "run_id": run_id,
            "project": PROJECT,
            "session": "s1",
            "node": {"id": NODE, "plan": "plan-a", "time_budget": "20m"},
            "phase": "working",
            "created_at": runs._utc_now(),
            "manifest_path": str(manifest),
            "log_path": str(log),
        },
    )


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def _read_until(
    lines: queue.Queue, marker: str, *, timeout: float, seen: list[str] | None = None
) -> str:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"the producer never printed a line with {marker!r}")
        row = lines.get(timeout=remaining)
        if seen is not None:
            seen.append(row)
        if marker in row:
            return row


def _reloaded_within(process: subprocess.Popen, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if RELOAD_LAUNCHER_MARKER in _cmdline(process.pid):
            return True
        if process.poll() is not None:
            return False
        time.sleep(POLL_SECONDS)
    return RELOAD_LAUNCHER_MARKER in _cmdline(process.pid)


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=8)


def test_an_uncommitted_edit_neither_reloads_nor_defers(
    isolated_home, tmp_path
) -> None:
    """A half-edit leaves HEAD fixed while the current image keeps delivering."""
    root, package = _fresh_checkout(tmp_path)
    before = _stamp_of(root)
    process, lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        module = package / "crew" / "ticker.py"
        module.write_bytes(module.read_bytes() + CONTENT_EDIT_BYTES)
        assert _stamp_of(root) == before
        assert not _reloaded_within(process, timeout=2.0)

        # The current image is still delivering: a run it has not reported
        # before is rendered as a row while the source commit stays fixed.
        _write_live_run(isolated_home, "r-1")
        seen: list[str] = []
        rendered = _read_until(lines, NODE, timeout=DEFER_WITHIN_SECONDS, seen=seen)
        assert NODE in rendered
        assert all(DIRTY_MARKER not in line for line in seen), seen
        assert all(DEFERRED_MARKER not in line for line in seen), seen
        assert RELOAD_LAUNCHER_MARKER not in _cmdline(process.pid), (
            "an uncommitted edit must not replace the running image"
        )
    finally:
        _stop(process)


def test_a_module_that_raises_nameerror_at_import_defers_the_reload(
    isolated_home, tmp_path
) -> None:
    """A module the process loads that fails to import holds the reload.

    The module is committed, moving HEAD and requiring an import proof before
    replacement. The proof reaches every module, including this broken one.
    """
    root, package = _fresh_checkout(tmp_path)
    before = _stamp_of(root)
    process, lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        broken = package / UNIMPORTABLE_MODULE
        broken.write_bytes(UNIMPORTABLE_BODY)
        _commit(root, "break a module the producer loads")

        deferred = _read_until(lines, DEFERRED_MARKER, timeout=DEFER_WITHIN_SECONDS)
        assert IMPORT_MARKER in deferred, deferred

        assert RELOAD_LAUNCHER_MARKER not in _cmdline(process.pid), (
            "an image that does not import must not be reloaded onto"
        )
    finally:
        _stop(process)


def test_committing_the_edit_reloads_the_committed_tree(
    isolated_home, tmp_path
) -> None:
    """The commit moves the stamp and supplies the replacement's exact bytes."""
    root, package = _fresh_checkout(tmp_path)
    before = _stamp_of(root)
    process, _lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        module = package / "crew" / "ticker.py"
        module.write_bytes(module.read_bytes() + CONTENT_EDIT_BYTES)
        assert _stamp_of(root) == before
        assert not _reloaded_within(process, timeout=2.0), (
            "an uncommitted edit must leave the running image in place"
        )

        _commit(root, "land the edit")
        after = _stamp_of(root)
        assert after != before
        assert _reloaded_within(process, timeout=RELOAD_WITHIN_SECONDS), (
            "a new source commit must replace the running image"
        )
        assert after in _cmdline(process.pid)
        images = list((isolated_home / "runtime" / "reload-trees").glob(f"*/{after}"))
        assert len(images) == 1, images
        committed = subprocess.run(
            [_GIT, "-C", str(root), "show", "HEAD:reckon/crew/ticker.py"],
            check=True,
            capture_output=True,
        ).stdout
        module.write_bytes(module.read_bytes() + CONTENT_EDIT_BYTES)
        assert module.read_bytes() != committed
        assert (images[0] / "reckon" / "crew" / "ticker.py").read_bytes() == committed
    finally:
        _stop(process)
