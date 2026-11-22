"""A review's filed record promotes the run when its manifest cannot be read.

A review run's whole deliverable is the record it files for the run it read, so
a manifest that does not parse strands a finished review on a file that says
nothing about it. These cases fix the behaviour and the refusals beside it:

- a review with a complete record and a one-byte manifest is promoted, both
  through the promotion command and through the unattended sweep, and its
  ledger row records the manifest it could not read;
- a review with an unreadable manifest and no record is refused as before, so
  an unreadable manifest is not on its own an exemption;
- an implement run with an unreadable manifest is refused as before, so the
  tolerance reaches reviews and reports only.

The fixture runs a temporary crew home and repository and asserts afterwards
that the workstation's real crew pointer directory is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon import ledger
from reckon.crew import promotion
from reckon.crew.node import CrewError
from reckon.crew.runs import _write_json, pointer_path
from tests.test_a_review_run_promotes_itself import (
    PLAN,
    PROJECT,
    REVIEW_NODE_ID,
    REVIEW_RUN_ID,
    _record_path,
    _store_review_record,
    _write_exit_record,
)
from tests.test_a_review_run_promotes_itself import repository as fixture_repository

repository = fixture_repository

UNREADABLE_MANIFEST = "x"


@pytest.fixture(autouse=True)
def real_crew_home_is_not_a_fixture_target() -> None:
    """No fixture may reach the workstation's real crew pointer directory."""
    real_pointer = (
        Path.home() / ".config" / "reckon" / "crew" / "live" / f"{REVIEW_RUN_ID}.json"
    )
    assert not real_pointer.exists()
    yield
    assert not real_pointer.exists()


def _write_pointer(
    root: Path,
    tmp_path: Path,
    *,
    manifest_text: str,
    record_path: Path | None,
    role: str = "review",
    node_id: str = REVIEW_NODE_ID,
) -> Path:
    """A live pointer for the review run, its manifest holding ``manifest_text``."""
    manifest = tmp_path / "manifests" / f"{REVIEW_RUN_ID}.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(manifest_text, encoding="utf-8")
    node: dict = {
        "id": node_id,
        "plan": PLAN,
        "section": "s2",
        "time_budget": "25m",
        "role": role,
    }
    if record_path is not None:
        node["write_paths"] = [str(record_path)]
    pointer = {
        "run_id": REVIEW_RUN_ID,
        "project": PROJECT,
        "repo": str(root),
        "launch": "in-harness",
        "role": role,
        "backend": "native",
        "created_at": "2026-10-12T13:00:00Z",
        "manifest_path": str(manifest),
        "node": node,
    }
    _write_json(pointer_path(REVIEW_RUN_ID), pointer)
    return manifest


def _promote(root: Path) -> dict:
    return promotion.complete(
        REVIEW_RUN_ID, gate="not-run", root=root, promoted_by="review-self"
    )


@pytest.mark.parametrize("route", ["promotion", "sweep"])
def test_a_complete_record_promotes_despite_a_one_byte_manifest(
    repository: Path, tmp_path: Path, route: str
) -> None:
    record_path = _store_review_record()
    manifest = _write_pointer(
        repository,
        tmp_path,
        manifest_text=UNREADABLE_MANIFEST,
        record_path=record_path,
    )
    _write_exit_record()

    if route == "sweep":
        from reckon.crew import recovery

        report = recovery.dispatch_awaiting_reviews(
            project=PROJECT, launcher=lambda *args, **kwargs: 0
        )
        assert report["self_promoted"] == [REVIEW_RUN_ID], report

    else:
        _promote(repository)

    assert not pointer_path(REVIEW_RUN_ID).exists()
    [row] = ledger.runs(PROJECT, root=repository)
    assert row["run_id"] == REVIEW_RUN_ID
    assert row["promoted_by"] == "review-self"
    assert row["manifest_unreadable"]["path"] == str(manifest)
    assert "cannot read manifest" in row["manifest_unreadable"]["reason"]


def test_an_unreadable_manifest_without_a_record_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    _write_pointer(
        repository,
        tmp_path,
        manifest_text=UNREADABLE_MANIFEST,
        record_path=_record_path(),
    )
    _write_exit_record()

    with pytest.raises(CrewError) as refusal:
        _promote(repository)

    assert "record cannot be read" in str(refusal.value)
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_an_implement_run_with_an_unreadable_manifest_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    record_path = _store_review_record()
    _write_pointer(
        repository,
        tmp_path,
        manifest_text=UNREADABLE_MANIFEST,
        record_path=record_path,
        role="implement",
        node_id="implement-the-reviewed-run",
    )
    _write_exit_record()

    with pytest.raises(CrewError) as refusal:
        _promote(repository)

    assert "cannot read manifest" in str(refusal.value)
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []


def test_the_unattended_sweep_does_not_promote_without_a_record(
    repository: Path, tmp_path: Path
) -> None:
    """The sweep leaves an unreadable manifest with no record as it found it."""
    from reckon.crew import recovery

    _write_pointer(
        repository,
        tmp_path,
        manifest_text=UNREADABLE_MANIFEST,
        record_path=_record_path(),
    )
    _write_exit_record()

    report = recovery.dispatch_awaiting_reviews(
        project=PROJECT, launcher=lambda *args, **kwargs: 0
    )

    assert report["self_promoted"] == [], json.dumps(report, indent=2)
    assert pointer_path(REVIEW_RUN_ID).is_file()
    assert ledger.runs(PROJECT, root=repository) == []
