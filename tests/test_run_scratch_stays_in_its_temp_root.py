"""A test's run scratch lands under its own temporary root.

A dispatch creates a private scratch directory beneath ``worker_scratch_root()``
and hands the worker its path as ``TMPDIR``. Without an override that root is
the node's real ``/tmp/reckon-crew-scratch``: a node-local directory shared by
every session on the machine, where an entry a test wrote is owned by nobody and
removed by nothing. The session fixture points ``RECKON_WORKER_SCRATCH_ROOT``
beneath the pytest base temp, so the scratch a dispatched run creates dies with
the tree pytest retains and prunes.

The declared mutation is the session fixture's assignment removed, applied by
running with ``RECKON_TEST_SCRATCH_ROOT_NEGATIVE=1``: a dispatch then resolves
the real root and writes into it, and both measures below must fail. The red
run removes exactly the directory it created, by name and only when the
directory was not already there, so demonstrating the defect adds no leak.
"""

from __future__ import annotations

import importlib
import os
import shutil
from pathlib import Path

# `reckon.crew.dispatch` is shadowed by the `dispatch` function the package
# re-exports, so the module is reached by importlib rather than by attribute.
dispatch = importlib.import_module("reckon.crew.dispatch")

NEGATIVE_CONTROL_MUTATION = (
    "remove the session fixture's RECKON_WORKER_SCRATCH_ROOT assignment, so a "
    "dispatch under the suite writes into the real root and the test fails"
)
NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_SCRATCH_ROOT_NEGATIVE") == "1"

RUN_ID = "r-20261004T101655975444-run-scratch-stays-in-its-temp-root"


def _real_scratch_root() -> Path:
    """The root dispatch resolves when nothing overrides it."""
    return (
        Path(dispatch.WORKER_SCRATCH_ROOT_DEFAULT) / dispatch.WORKER_SCRATCH_ROOT_NAME
    )


def _entries(root: Path) -> set[str]:
    """The names of ``root``'s direct children; an absent root holds none."""
    try:
        return {child.name for child in root.iterdir()}
    except FileNotFoundError:
        return set()


def test_the_session_points_run_scratch_beneath_its_base_temp(tmp_path_factory) -> None:
    """The override is set, and its root sits beneath the session temp root."""
    base = tmp_path_factory.getbasetemp()
    root = dispatch.worker_scratch_root()

    assert os.environ.get(dispatch.WORKER_SCRATCH_ROOT_ENV) == str(root)
    assert base in root.parents, f"{root} is not beneath the session base temp {base}"
    assert root != _real_scratch_root(), "the session must not resolve the real root"


def test_a_dispatch_writes_its_scratch_beneath_the_session_temp_root(
    tmp_path: Path, tmp_path_factory, monkeypatch
) -> None:
    """A dispatch's scratch is under the base temp, and the real root gains nothing.

    An absence claim needs its instrument shown to see something known present
    first, so the reader that reports the real root unchanged is the same one
    that reads back a probe written here.
    """
    base = tmp_path_factory.getbasetemp()
    real_root = _real_scratch_root()
    probe = tmp_path / "instrument-probe.txt"
    probe.write_text("probe\n", encoding="utf-8")
    assert probe.name in _entries(tmp_path), "the entries reader sees a known entry"
    before = _entries(real_root)

    if NEGATIVE_CONTROL:
        # The declared mutation: the fixture's assignment is removed, so the
        # dispatch below resolves the real root exactly as the unfixed suite did.
        monkeypatch.delenv(dispatch.WORKER_SCRATCH_ROOT_ENV, raising=False)

    scratch: Path | None = None
    try:
        runtime = dispatch._worker_runtime_environment(
            {},
            run_id=RUN_ID,
            manifest_path="/nonexistent/manifest.md",
            attempt_started_at="2026-10-04T10:17:10Z",
            coordinator_session="s22-coord",
            claude_headers=False,
        )
        scratch = Path(runtime["TMPDIR"])
        (scratch / "worker-probe.txt").write_text("probe\n", encoding="utf-8")
        after = _entries(real_root)
        print(f"run scratch root: {dispatch.worker_scratch_root()}")
        print(f"dispatch scratch directory: {scratch}")
        print(f"real root gained: {sorted(after - before)}")

        assert scratch.name == RUN_ID
        assert base in scratch.parents, (
            f"a dispatch created {scratch}, which is not beneath the session "
            f"base temp {base}"
        )
        assert after == before, (
            f"the real scratch root {real_root} gained {sorted(after - before)}"
        )
    finally:
        # Under the mutation this run performed the real-root write the defect
        # is about; remove exactly that directory again, by its name and only
        # when it was not already there, so no entry is invented or taken.
        if (
            scratch is not None
            and scratch.parent == real_root
            and RUN_ID not in before
            and scratch.is_dir()
            and not scratch.is_symlink()
        ):
            shutil.rmtree(scratch)
            print(f"removed the real-root directory this run created: {scratch}")
