"""A fenced worker cannot remove another run's reports or reviews.

Two delivery stores are shared between every run: the reports root
(``reports_dir()``, normally ``~/.config/reckon/crew/reports``) and the review
store (``review_store_root()``, ``…/crew/reviews``). The fence dev-binds ``/``
writable and overlays only the declared protected paths read-only, and the fence
handed every launch the whole of both stores as writable roots, so any worker
could write — and delete — every other project's and every other run's reports
and reviews. One run's cleanup did exactly that on 2026-10-16: an ``rm -rf`` of a
mistyped path removed the whole reports root, and the run's ``worker.json``
records a writable ``--bind`` of both stores.

The fence closes this the way it closes the worker scratch root: each shared
store is sealed read-only among the protected paths, and only the run's *own*
declared paths beneath it are re-bound writable by the ordinary grant that
follows. A worker can still read every report — a read-only bind withholds
writes, not reads.

The property is asserted three ways, because a shape assertion cannot tell a
fence that holds from one spelled correctly and doing nothing, and an executed
refusal cannot tell which overlay produced it.

* The outline is asserted on the *composed argv*: each store root is overlaid
  read-only, and the run's declared directory beneath it is bound writable
  **after** the overlay, with no writable grant for the whole store nor for a
  sibling's tree.
* The hand-off is asserted on ``_fence_write_roots``: the run's declared report
  and review directories are among the roots the fence re-binds, so a composed
  plan review or a run review whose delivery path lies under either store keeps
  it writable even though the store root itself is sealed.
* The fence holds, proved by *executing* the composed argv: a fenced command
  writes and deletes a file in its own declared directories (lands), and
  ``rm -rf`` of a sibling report tree or ``rm`` of a sibling review record exits
  non-zero with the sibling still present.

Two declared negative controls each strip one store from the fence's sealed set
— patching the function that decides it — so no read-only overlay of that store
is emitted; the fenced ``rm`` then deletes the sibling and the executed test
fails. They are applied by running with ``RECKON_TEST_REPORTS_SEAL_NEGATIVE=1``
and, separately, ``RECKON_TEST_REVIEW_SEAL_NEGATIVE=1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _worker_fence
from reckon.crew import dispatch_admission

REPORTS_NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_REPORTS_SEAL_NEGATIVE") == "1"
REVIEWS_NEGATIVE_CONTROL = os.environ.get("RECKON_TEST_REVIEW_SEAL_NEGATIVE") == "1"

NEGATIVE_CONTROL_MUTATION = (
    "with the reports root, and separately the review store, removed from the "
    "sealed set — patch shared_store_seal_roots to drop that store — so no "
    "read-only overlay of it is emitted; the fenced rm then deletes the sibling "
    "report tree or review record and the test fails"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

RUN_ID = "r-20261016T120000000000-under-test"
SIBLING_ID = "r-20261016T120500000000-sibling-run"
PROJECT = "reckon"

# Removes a sibling report tree as its final command, so the command's exit code
# is the removal's. It first proves its own declared report directory is
# writable by writing and deleting a file there, writing the verdict to a file
# only the fence's own grant admits.
_REPORTS_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN_REPORT/probe" 2>/dev/null && rm -f "$FENCE_OWN_REPORT/probe" 2>/dev/null; then
  echo "own-write-delete-ok" >> "$FENCE_RESULTS"
else
  echo "own-write-delete-failed" >> "$FENCE_RESULTS"
fi
rm -rf "$FENCE_SIBLING_REPORT"
"""

# The review sibling case: writes and deletes in its own declared review
# directory, then removes a sibling run's stored review record.
_REVIEWS_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if touch "$FENCE_OWN_REVIEW/probe" 2>/dev/null && rm -f "$FENCE_OWN_REVIEW/probe" 2>/dev/null; then
  echo "own-write-delete-ok" >> "$FENCE_RESULTS"
else
  echo "own-write-delete-failed" >> "$FENCE_RESULTS"
fi
rm -f "$FENCE_SIBLING_REVIEW"
"""


class Stores:
    """A synthetic crew home: one reports root, one review store, a run and a sibling."""

    def __init__(self, root: Path) -> None:
        self.base = root
        self.home = root / "home"
        self.crew_home = root / "crew"
        self.reports_root = self.crew_home / "reports"
        self.reviews_root = self.crew_home / "reviews"
        # The run's own declared report directory, a plan review's leaf under a
        # plan slug, and a sibling review's leaf under a different slug.
        self.own_report = (
            self.reports_root / PROJECT / "plan-review" / "slug-a" / RUN_ID
        )
        self.sibling_report = (
            self.reports_root / PROJECT / "plan-review" / "slug-b" / SIBLING_ID
        )
        # The run's own declared review directory (a record's parent) and a
        # sibling run's record under a different subject directory.
        self.own_review = self.reviews_root / PROJECT / "run-a"
        self.sibling_review = (
            self.reviews_root / PROJECT / "run-b" / f"{SIBLING_ID}.json"
        )
        self.run = self.crew_home / "runs" / RUN_ID
        self.manifest = self.run / "manifest.md"
        self.results = self.run / "results.txt"
        self.home.mkdir(parents=True, exist_ok=True)
        self.own_report.mkdir(parents=True)
        self.sibling_report.mkdir(parents=True)
        (self.sibling_report / "keep.txt").write_text("sibling\n")
        self.own_review.mkdir(parents=True)
        self.sibling_review.parent.mkdir(parents=True)
        self.sibling_review.write_text("{}\n")
        self.run.mkdir(parents=True)
        self.manifest.write_text("manifest\n")

    def point_the_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Resolve the crew home to this tree, with a scratch root beside it."""
        monkeypatch.setenv("RECKON_HOME", str(self.base))
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.base / "scratch"))

    def argv(self, *, stub: Path, writable: list[str]) -> list[str]:
        """Compose the fence the way a dispatch does, through the production path."""
        return _worker_fence.fence_argv(
            [str(stub)],
            writable_directories=writable,
            manifest_path=str(self.manifest),
            home=str(self.home),
        )

    def write_stub(self, text: str) -> Path:
        stub = self.base / "stub.sh"
        stub.write_text(text)
        stub.chmod(0o755)
        return stub

    def run_fence(
        self, argv: list[str], extra_env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        env = {"HOME": str(self.home), "FENCE_RESULTS": str(self.results), **extra_env}
        return subprocess.run(
            argv,
            env=env,
            cwd=str(self.base),
            capture_output=True,
            text=True,
            check=False,
        )

    def results_text(self) -> str:
        return self.results.read_text()


def _index_of(argv: list[str], kind: str, destination: Path) -> int:
    """Return the index of the ``kind`` mount of ``destination``, or -1."""
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == kind and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _bind_of(argv: list[str], destination: Path) -> int:
    return _index_of(argv, "--bind", destination)


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    return _index_of(argv, "--ro-bind", destination)


def test_the_reports_root_is_sealed_before_the_declared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reports root is overlaid read-only, the declared dir granted after it."""
    stores = Stores(tmp_path)
    stores.point_the_stores(monkeypatch)
    argv = stores.argv(
        stub=stores.write_stub(_REPORTS_STUB), writable=[str(stores.own_report)]
    )
    root_overlay = _ro_bind_of(argv, stores.reports_root)
    own_bind = _bind_of(argv, stores.own_report)
    # Positive control: the root is actually overlaid, so an absent overlay would
    # mean the assertions below are testing nothing rather than passing.
    assert root_overlay != -1, "the reports root was not overlaid read-only"
    assert own_bind != -1, "the run's declared report dir was not bound writable"
    assert own_bind > root_overlay, (
        "the declared-dir grant precedes the reports-root overlay, so the "
        "read-only bind would shadow it"
    )
    # The whole store is never re-opened, and no sibling tree is granted.
    assert _bind_of(argv, stores.reports_root) == -1, (
        "the whole reports root was bound writable, re-opening every run's reports"
    )
    assert _bind_of(argv, stores.sibling_report) == -1, (
        "a sibling report tree was bound writable"
    )


def test_the_review_store_is_sealed_before_the_declared_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review store is overlaid read-only, the declared dir granted after it."""
    stores = Stores(tmp_path)
    stores.point_the_stores(monkeypatch)
    argv = stores.argv(
        stub=stores.write_stub(_REVIEWS_STUB), writable=[str(stores.own_review)]
    )
    root_overlay = _ro_bind_of(argv, stores.reviews_root)
    own_bind = _bind_of(argv, stores.own_review)
    assert root_overlay != -1, "the review store was not overlaid read-only"
    assert own_bind != -1, "the run's declared review dir was not bound writable"
    assert own_bind > root_overlay, (
        "the declared-dir grant precedes the review-store overlay"
    )
    assert _bind_of(argv, stores.reviews_root) == -1, (
        "the whole review store was bound writable, re-opening every stored review"
    )


def test_the_declared_directories_reach_the_fence_as_write_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared report and review dirs are roots the fence re-binds; the stores are not."""
    stores = Stores(tmp_path)
    stores.point_the_stores(monkeypatch)
    roots = {
        Path(root).resolve()
        for root in dispatch_admission._fence_write_roots(
            backend={"launch": "cli", "command": "true", "sandbox": "worktree-full"},
            repository=str(tmp_path / "repo"),
            run_directory=str(stores.run),
            manifest_path=str(stores.manifest),
            worktree=None,
            declared_write_paths=(str(stores.own_report), str(stores.own_review)),
        )
    }
    assert stores.own_report.resolve() in roots, (
        f"{stores.own_report} is not among the fence write roots "
        f"{sorted(map(str, roots))}"
    )
    assert stores.own_review.resolve() in roots, (
        f"{stores.own_review} is not among the fence write roots "
        f"{sorted(map(str, roots))}"
    )
    # The shared stores themselves reach the fence as roots too (they are the
    # delivery roots a restricted tier gets), but the fence seals each of them
    # read-only and skips re-binding it, so the grant loop can never re-open a
    # whole store through the seal; the composed-argv tests above assert that
    # neither store root is bound writable.


@pytest.fixture
def store_seal_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative controls when the env vars ask for them.

    Each arm removes one store from the sealed set exactly as the control string
    states: the seam that decides which stores are sealed no longer returns that
    store, so no read-only overlay of it is composed.
    """
    if not (REPORTS_NEGATIVE_CONTROL or REVIEWS_NEGATIVE_CONTROL):
        return

    from reckon.crew.review import review_store_root
    from reckon.crew.runs import reports_dir

    real = _worker_fence.shared_store_seal_roots

    def mutated() -> list[Path]:
        seals = list(real())
        if REPORTS_NEGATIVE_CONTROL:
            drop = reports_dir().resolve()
            seals = [p for p in seals if p.resolve() != drop]
        if REVIEWS_NEGATIVE_CONTROL:
            drop = review_store_root().resolve()
            seals = [p for p in seals if p.resolve() != drop]
        return seals

    monkeypatch.setattr(_worker_fence, "shared_store_seal_roots", mutated)


@requires_bwrap
def test_a_fenced_run_cannot_remove_a_siblings_report_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_seal_mutation: None,
) -> None:
    """The sibling report removal is refused read-only; the own write lands."""
    stores = Stores(tmp_path)
    stores.point_the_stores(monkeypatch)
    stub = stores.write_stub(_REPORTS_STUB)
    argv = stores.argv(stub=stub, writable=[str(stores.own_report)])
    proc = stores.run_fence(
        argv,
        {
            "FENCE_OWN_REPORT": str(stores.own_report),
            "FENCE_SIBLING_REPORT": str(stores.sibling_report),
        },
    )
    results = stores.results_text()
    # Positive control: the stub writes its record only from inside the fence, so
    # a non-empty record proves the shim ran and the own report dir was writable.
    assert "own-write-delete-ok" in results, results
    assert not (stores.own_report / "probe").exists()
    assert proc.returncode != 0, (
        f"the fenced rm of a sibling report tree exited {proc.returncode}, not "
        f"non-zero; stderr: {proc.stderr!r}"
    )
    assert stores.sibling_report.is_dir(), "the sibling report tree was removed"
    assert (stores.sibling_report / "keep.txt").exists()


@requires_bwrap
def test_a_fenced_run_cannot_remove_a_siblings_review_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_seal_mutation: None,
) -> None:
    """The sibling review removal is refused read-only; the own write lands."""
    stores = Stores(tmp_path)
    stores.point_the_stores(monkeypatch)
    stub = stores.write_stub(_REVIEWS_STUB)
    argv = stores.argv(stub=stub, writable=[str(stores.own_review)])
    proc = stores.run_fence(
        argv,
        {
            "FENCE_OWN_REVIEW": str(stores.own_review),
            "FENCE_SIBLING_REVIEW": str(stores.sibling_review),
        },
    )
    results = stores.results_text()
    assert "own-write-delete-ok" in results, results
    assert not (stores.own_review / "probe").exists()
    assert proc.returncode != 0, (
        f"the fenced rm of a sibling review record exited {proc.returncode}, not "
        f"non-zero; stderr: {proc.stderr!r}"
    )
    assert stores.sibling_review.is_file(), "the sibling review record was removed"
