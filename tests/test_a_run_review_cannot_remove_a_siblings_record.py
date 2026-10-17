"""A run review is granted no writable directory holding another run's record.

The review store keeps every run of a project as a flat file in one directory,
``<review_store_root>/<project>/<run>.json`` beside ``<run>.at-<head>.json``.
A run review declared exactly those two paths, and neither exists when the
fence is composed, so ``_declared_write_grant`` granted the file through its
parent — the project's own directory. Measured 2026-10-16 at 22:13Z, that
directory held 4,233 records, and every review launch held it writable.

A run review now declares its record inside ``review-records/`` in the
directory of the run it reviews, and the record is filed into the store when
the run ends. The property is asserted three ways, because a shape assertion
cannot tell a fence that holds from one spelled correctly and doing nothing,
and an executed refusal cannot tell which grant produced it.

* the declared paths are composed through the production composer and asserted
  to land in that directory, and not under the store;
* the composed argv binds the record directory and binds no directory that
  holds another run's record — neither the project directory nor the store;
* the composed fence is *executed*: a fenced command writes its own record
  (lands) and its ``rm`` of a sibling's flat record exits non-zero with the
  sibling still present.

Three declared negative controls each strip one seam, run through the
environment variables ``RECKON_TEST_REVIEW_RECORD_GRANT_NEGATIVE``,
``RECKON_TEST_REVIEW_PATHS_NEGATIVE`` and ``RECKON_TEST_REVIEW_FILING_NEGATIVE``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from reckon import _worker_fence
from reckon.crew import (
    dispatch_admission,
    promotion,
    recovery_review_delivery,
    recovery_review_dispatch,
    recovery_review_subject,
)
from reckon.crew import review as review_module

GRANT_NEGATIVE = os.environ.get("RECKON_TEST_REVIEW_RECORD_GRANT_NEGATIVE") == "1"
PATHS_NEGATIVE = os.environ.get("RECKON_TEST_REVIEW_PATHS_NEGATIVE") == "1"
FILING_NEGATIVE = os.environ.get("RECKON_TEST_REVIEW_FILING_NEGATIVE") == "1"

NEGATIVE_CONTROL_MUTATION = (
    "widening the grant back to the parent directory lets the sibling removal "
    "succeed; removing the fallback filing leaves the record unfiled; leaving "
    "_review_store_record_paths store-only returns an empty reviewed head"
)

requires_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap is not installed"
)

PROJECT = "reckon"
REVIEWED_ID = "r-20261016T120000000000-under-test"
REVIEW_ID = "r-20261016T121000000000-review-of-under-test"
SIBLING_ID = "r-20261016T120500000000-sibling-run"
HEAD = "a" * 7
OLDER_HEAD = "b" * 7

# Writes a record into its own declared record directory and then removes a
# sibling run's flat record from the project review directory. The verdict file
# is written first, so a fence that refuses the *write* is still distinguishable
# from one that refuses the removal.
_REVIEW_STUB = """#!/bin/sh
set -u
: > "$FENCE_RESULTS"
if printf '{"project": "reckon"}\\n' > "$FENCE_OWN_RECORD" 2>/dev/null; then
  echo "own-record-write-ok" >> "$FENCE_RESULTS"
else
  echo "own-record-write-failed" >> "$FENCE_RESULTS"
fi
rm -f "$FENCE_SIBLING_RECORD"
"""


def _reviewed_record() -> dict:
    """A reviewed run's own pointer, as the composer reads it."""
    return {
        "run_id": REVIEWED_ID,
        "project": PROJECT,
        "node": {"id": "under-test", "time_budget": "20m"},
    }


class Store:
    """A synthetic crew home with a flat review store and one review's runs."""

    def __init__(self, root: Path) -> None:
        self.base = root
        self.home = root / "home"
        self.crew_home = root / "crew"
        self.reviews_root = self.crew_home / "reviews"
        self.project_directory = self.reviews_root / PROJECT
        self.sibling_record = self.project_directory / f"{SIBLING_ID}.json"
        self.manifest = self.crew_home / "runs" / REVIEW_ID / "manifest.md"
        self.results = self.crew_home / "runs" / REVIEW_ID / "results.txt"
        self.home.mkdir(parents=True, exist_ok=True)
        self.project_directory.mkdir(parents=True)
        self.sibling_record.write_text('{"project": "reckon"}\n')
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text("status: complete\n")

    def point_the_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RECKON_HOME", str(self.base))
        monkeypatch.setenv("RECKON_WORKER_SCRATCH_ROOT", str(self.base / "scratch"))
        (self.base / "scratch" / REVIEW_ID).mkdir(parents=True, exist_ok=True)

    def reviewed_run_directory(self) -> Path:
        from reckon.crew.runs import run_dir

        return Path(run_dir(REVIEWED_ID))

    def record_directory(self) -> Path:
        return review_module.run_review_record_directory(self.reviewed_run_directory())

    def composition(self) -> dict:
        """The run review's dispatch fields, composed through the production path."""
        return recovery_review_subject._review_dispatch_fields(
            _reviewed_record(), delta=None
        )

    def fence_roots(self, write_paths) -> tuple:
        return dispatch_admission._fence_write_roots(
            backend={"launch": "cli", "command": "true", "sandbox": "worktree-full"},
            repository=str(self.base / "repo"),
            run_directory=str(self.manifest.parent),
            manifest_path=str(self.manifest),
            worktree=None,
            declared_write_paths=tuple(write_paths),
        )

    def composed_argv(self, write_paths, *, stub: Path) -> list[str]:
        return _worker_fence.fence_argv(
            [str(stub)],
            writable_directories=self.fence_roots(write_paths),
            manifest_path=str(self.manifest),
            home=str(self.home),
        )

    def write_stub(self) -> Path:
        stub = self.base / "stub.sh"
        stub.write_text(_REVIEW_STUB)
        stub.chmod(0o755)
        return stub


def _index_of(argv: list[str], kind: str, destination: Path) -> int:
    target = str(_worker_fence.resolved_destination(destination))
    for index, part in enumerate(argv):
        if part == kind and index + 2 < len(argv) and argv[index + 1] == target:
            return index
    return -1


def _bind_of(argv: list[str], destination: Path) -> int:
    return _index_of(argv, "--bind", destination)


def _ro_bind_of(argv: list[str], destination: Path) -> int:
    return _index_of(argv, "--ro-bind", destination)


@pytest.fixture
def mutations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the declared negative control the environment asks for."""
    if GRANT_NEGATIVE:
        # The widening, which is what a store-path declaration composes: the
        # record is declared where it does not exist yet, so the fence grants
        # its parent — the project directory holding every other run's record.
        def widened(run_directory, reviewed_run_id, *, reviewed_head_sha=""):
            paths = [str(review_module.review_path(PROJECT, REVIEWED_ID))]
            if reviewed_head_sha:
                paths.append(
                    str(
                        review_module.review_path(
                            PROJECT, REVIEWED_ID, reviewed_head_sha=reviewed_head_sha
                        )
                    )
                )
            return paths

        monkeypatch.setattr(
            review_module, "run_review_record_paths", widened, raising=True
        )
    if PATHS_NEGATIVE:

        def store_only(record, project):
            node = record.get("node") or {}
            directory = review_module.review_store_root() / project
            return [
                Path(str(declared))
                for declared in node.get("write_paths") or ()
                if Path(str(declared)).parent == directory
            ]

        monkeypatch.setattr(
            recovery_review_delivery,
            "_review_store_record_paths",
            store_only,
            raising=True,
        )
        # The dispatch module imports the reader by name, so a control applied
        # at its definition alone would never reach the subject reader.
        monkeypatch.setattr(
            recovery_review_dispatch,
            "_review_store_record_paths",
            store_only,
            raising=True,
        )
    if FILING_NEGATIVE:
        monkeypatch.setattr(
            recovery_review_delivery,
            "file_review_records_into_store",
            lambda record: [],
            raising=True,
        )


def test_a_run_review_declares_its_record_in_the_reviewed_runs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """The declared paths land in the run's record directory, not in the store."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    write_paths = store.composition()["write_paths"]
    declared = [Path(path) for path in write_paths]
    assert declared, "the run review declared no record path at all"
    for path in declared:
        assert path.is_absolute(), f"{path} is not an absolute declaration"
        assert path.parent == store.record_directory(), (
            f"{path} is not inside {store.record_directory()}"
        )
        assert path.parent.parent == store.reviewed_run_directory()
        assert store.project_directory not in path.parents, (
            f"{path} is declared under the store's project directory"
        )
    assert store.record_directory().is_dir(), (
        "the record directory was not brought into being, so the fence would "
        "grant the whole run directory above it"
    )
    assert declared[0].name == f"{REVIEWED_ID}.json"


def test_the_composed_fence_binds_no_directory_holding_another_runs_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """The record directory is bound; the store and its project directory are not."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    argv = store.composed_argv(
        store.composition()["write_paths"], stub=store.write_stub()
    )
    # Positive control first: the store really is overlaid read-only, so the
    # two absences below are the seal holding rather than an empty composition.
    assert _ro_bind_of(argv, store.reviews_root) != -1, (
        "the review store was not overlaid read-only at all"
    )
    assert _ro_bind_of(argv, store.record_directory()) == -1, (
        "the review's own record directory was overlaid read-only, so the "
        "record could not be delivered"
    )
    assert _bind_of(argv, store.project_directory) == -1, (
        "the project's review directory — holding every other run's record — "
        "was bound writable"
    )
    assert _bind_of(argv, store.reviews_root) == -1, (
        "the whole review store was bound writable"
    )
    # Positive control: the store really is handed to the fence as a root, so
    # the two absences above are the seal holding rather than the root list
    # never reaching it.
    roots = {
        Path(root).resolve()
        for root in store.fence_roots(store.composition()["write_paths"])
    }
    assert store.record_directory().resolve() in roots, (
        "the record directory was not granted, so the record could not be "
        f"delivered: {sorted(map(str, roots))}"
    )
    assert store.reviewed_run_directory().resolve() not in roots, (
        "the reviewed run's own directory was granted writable, so a review "
        "could rewrite the run it reviews"
    )


def test_the_record_directory_exists_before_the_fence_is_composed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """The composer creates the record directory, so the grant cannot widen.

    A declared path whose directory does not exist is granted through its
    nearest existing ancestor, which here is the reviewed run's own directory —
    holding that run's manifest, pointer and streams. The directory must exist
    by the time the fence is composed for the dispatch, and a read must not
    bring it into being.
    """
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    directory = store.record_directory()
    assert not directory.exists(), "the fixture created the record directory"
    # A read composes the same paths and writes nothing, so it leaves no
    # directory behind for the run directory to be reached through.
    read = recovery_review_subject._review_dispatch_fields(_reviewed_record())
    assert not directory.exists(), "a read brought the record directory into being"
    dispatched = recovery_review_subject._review_dispatch_fields(
        _reviewed_record(), delta=None
    )
    assert read["write_paths"] == dispatched["write_paths"], (
        "a read and a dispatch disagreed about the declared record paths"
    )
    assert directory.is_dir(), (
        "the composer left the record directory uncreated, so the fence would "
        "grant the reviewed run's whole directory to reach the declaration"
    )
    roots = {
        Path(root).resolve() for root in store.fence_roots(dispatched["write_paths"])
    }
    assert directory.resolve() in roots
    assert store.reviewed_run_directory().resolve() not in roots


@requires_bwrap
def test_a_fenced_review_removes_no_siblings_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """The fenced command writes its own record and cannot remove a sibling's."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    argv = store.composed_argv(
        store.composition()["write_paths"], stub=store.write_stub()
    )
    env = {
        "HOME": str(store.home),
        "FENCE_RESULTS": str(store.results),
        "FENCE_OWN_RECORD": str(Path(store.composition()["write_paths"][0])),
        "FENCE_SIBLING_RECORD": str(store.sibling_record),
    }
    completed = subprocess.run(
        argv, env=env, cwd=str(store.base), capture_output=True, text=True, check=False
    )
    results = store.results.read_text() if store.results.exists() else ""
    assert "own-record-write-ok" in results, (
        f"the fenced command could not write its own record: {results!r} "
        f"stderr={completed.stderr!r}"
    )
    assert completed.returncode != 0, (
        "removing a sibling run's record exited zero, so the project directory "
        "was writable"
    )
    assert store.sibling_record.exists(), (
        "the sibling run's record was removed by another run's review"
    )


def _delivered_body() -> dict:
    """A complete review record, as a reviewer writes one into its run directory."""
    return {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_ID,
        "reviewed_base_sha": "c" * 7,
        "reviewed_head_sha": HEAD,
        "status": "parsed",
        "scores": dict.fromkeys(review_module.REVIEW_DIMENSIONS, 4),
        "total": 4 * len(review_module.REVIEW_DIMENSIONS),
    }


def _deliver(store: Store, body: dict) -> Path:
    """Write a review run's record where its declaration says it goes."""
    from reckon.crew.runs import run_dir

    directory = review_module.run_review_record_directory(Path(run_dir(REVIEWED_ID)))
    path = directory / f"{REVIEWED_ID}.at-{HEAD}.json"
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body))
    return path


def _review_pointer(store: Store) -> dict:
    """A live review pointer declaring the record paths the composer declares."""
    from reckon.crew.runs import run_dir

    declared = review_module.run_review_record_paths(
        Path(run_dir(REVIEWED_ID)), REVIEWED_ID, reviewed_head_sha=HEAD
    )
    return {
        "run_id": REVIEW_ID,
        "project": PROJECT,
        "role": "review",
        "manifest_path": str(store.manifest),
        "node": {"id": f"review-of-{REVIEWED_ID}", "write_paths": declared},
    }


def test_a_delivered_record_is_filed_into_the_store_and_reads_as_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """A record delivered into a run directory reaches the store and reads present."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _deliver(store, _delivered_body())
    pointer = _review_pointer(store)
    filed = recovery_review_delivery.file_review_records_into_store(pointer)
    expected = review_module.review_path(PROJECT, REVIEWED_ID, reviewed_head_sha=HEAD)
    assert filed == [expected], f"filed {filed} rather than {expected}"
    assert expected.is_file(), "the record was not filed at the store's own path"
    delivered = recovery_review_delivery.review_delivered(pointer)
    assert delivered is not None, "the filed review does not read as delivered"
    # The declared path is read first, which is the point of delivering through
    # the run directory: the live reader finds the reviewer's own record there,
    # and the store copy is what every other reader finds unchanged.
    assert delivered["record_path"] == pointer["node"]["write_paths"][1]


def test_an_unfiled_record_is_filed_by_the_runs_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """A supervisor that died before filing leaves a record completion still files."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _deliver(store, _delivered_body())
    pointer = _review_pointer(store)
    expected = review_module.review_path(PROJECT, REVIEWED_ID, reviewed_head_sha=HEAD)
    assert not expected.exists(), "the record reached the store before completion ran"
    written = promotion._file_delivered_run_reviews(pointer)
    assert written == [str(expected)], (
        f"completion filed {written} rather than {expected}"
    )
    assert expected.is_file()
    assert recovery_review_delivery.review_delivered(pointer) is not None


def test_a_review_subject_survives_the_move_and_its_head_is_not_covered_by_an_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutations: None
) -> None:
    """The declared paths still name the reviewed run and head, and heads separate."""
    store = Store(tmp_path)
    store.point_the_stores(monkeypatch)
    _deliver(store, _delivered_body())
    pointer = _review_pointer(store)
    reviewed, head = recovery_review_dispatch._review_subject(pointer, PROJECT)
    assert reviewed == REVIEWED_ID, f"_review_subject returned {reviewed!r}"
    assert head == HEAD, f"_review_subject returned head {head!r}"
    assert recovery_review_dispatch._review_head_covers(HEAD, HEAD)
    assert not recovery_review_dispatch._review_head_covers(OLDER_HEAD, HEAD), (
        "a review of an older head covered a newer one"
    )
