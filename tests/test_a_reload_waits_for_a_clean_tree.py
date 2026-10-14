"""A reload waits for a clean, importable tree.

A producer or follower replaces its own process image when the content-hash
stamp over its source moves. A tree mid-edit can move that stamp while it is
still not fit to run: a session edited ``reckon/crew/ticker.py`` and
``reckon/crew/recovery_watch.py`` in place in the shared checkout, and every
project's producer — which imports that checkout — reloaded onto the half-edit.
One failed at import with a bare name; another imported cleanly and failed only
when a row was rendered, because a rename was applied at the call site and not
at the definition. So the reload is deferred while any tracked file under
``reckon/`` differs from ``HEAD``, and while any module the process loads fails
to import. A deferred reload keeps the running image, says so once, and retries
— when the code moves again, or when the tree is committed back to clean.

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

# The reload's own line, and the phrase each deferral reason carries. The two
# reasons are distinguished so a case proves the guard it means to.
DEFERRED_MARKER = "reckon crew watch deferred its reload"
DIRTY_MARKER = "modified against HEAD"
IMPORT_MARKER = "does not import"

# The replacement image is launched with a launcher that inserts its import
# root on ``sys.path`` before entering the command. That string is absent from
# the argv the producer is armed with, so finding it on the process is direct
# evidence the image was replaced.
RELOAD_LAUNCHER_MARKER = "sys.path.insert"

# A comment, and one whose every prefix is a comment, so the module still parses
# and the change is content-only: the stamp moves while the tree remains fit to
# import, which is exactly the half-edit a dirty tree catches and the import
# proof does not.
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

    A reload is deferred while a tracked file differs from ``HEAD``, so a case
    that forces a reload by changing the source cannot edit the package its own
    process imports: that leaves the tree modified and the reload is held back.
    The case changes a copy of its own and commits the change, so the tree stays
    clean against ``HEAD`` and the only thing that moved is the content stamp.
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
    """Compute the content-hash stamp over a copied tree, in its own process."""
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


def _read_until(lines: queue.Queue, marker: str, *, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"the producer never printed a line with {marker!r}")
        row = lines.get(timeout=remaining)
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


def test_a_tracked_file_modified_against_head_defers_the_reload(
    isolated_home, tmp_path
) -> None:
    """A half-edit left in the tree holds the reload, and the pane keeps flowing.

    The edit is content-only and parses, so the import proof passes it: the dirt
    against ``HEAD`` is the only thing that can defer the reload, which is what
    a rename applied at the call site and not the definition leaves behind. The
    running image must stay on the pane, because deferring onto a dead pane
    would be the darkening the deferral exists to prevent.
    """
    root, package = _fresh_checkout(tmp_path)
    before = _stamp_of(root)
    process, lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        module = package / "crew" / "ticker.py"
        module.write_bytes(module.read_bytes() + CONTENT_EDIT_BYTES)

        deferred = _read_until(lines, DEFERRED_MARKER, timeout=DEFER_WITHIN_SECONDS)
        assert DIRTY_MARKER in deferred, deferred

        # The current image is still delivering: a run it has not reported
        # before is rendered as a row while the reload stays held.
        _write_live_run(isolated_home, "r-1")
        rendered = _read_until(lines, NODE, timeout=DEFER_WITHIN_SECONDS)
        assert NODE in rendered

        assert RELOAD_LAUNCHER_MARKER not in _cmdline(process.pid), (
            "the modified-file reload must be deferred, not performed"
        )
    finally:
        _stop(process)


def test_a_module_that_raises_nameerror_at_import_defers_the_reload(
    isolated_home, tmp_path
) -> None:
    """A module the process loads that fails to import holds the reload.

    The unimportable module is written and committed, so the tree is clean
    against ``HEAD`` and the dirty-tree guard cannot be what defers: only the
    import proof, which reaches every module and not just the entry point, can.
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


def test_committing_the_edit_lets_the_reload_proceed(isolated_home, tmp_path) -> None:
    """Cleaning the tree against ``HEAD`` releases the held reload.

    Committing the edit leaves every byte where it was, so the code stamp the
    reload was deferred at does not move: the gate must re-read the tree rather
    than the stamp, or the reload would stay held forever.
    """
    root, package = _fresh_checkout(tmp_path)
    before = _stamp_of(root)
    process, lines = _launch_producer(root, isolated_home)
    try:
        _await_seat(PROJECT, before)

        module = package / "crew" / "ticker.py"
        module.write_bytes(module.read_bytes() + CONTENT_EDIT_BYTES)

        _read_until(lines, DEFERRED_MARKER, timeout=DEFER_WITHIN_SECONDS)
        assert not _reloaded_within(process, timeout=2.0), (
            "a dirty tree must hold the reload before the commit"
        )

        _commit(root, "land the edit")

        assert _reloaded_within(process, timeout=RELOAD_WITHIN_SECONDS), (
            "a committed-clean tree must let the reload proceed"
        )
    finally:
        _stop(process)
