"""The review store owns a record's identity, its times, and what it accepts.

A committed record's times are the times of the run that produced it, and the
store resolves them itself rather than trusting the clock. A record whose review
run has no committed per-run row of its own is still committed: the store takes
the dispatch and completion stamps the record carries, or, failing those, the
dispatch instant its review run id encodes — recording which stage it used under
``times_source`` and never substituting the store clock. A plan review's review
run id is the composed id its report directory is named for, so the store
resolves the crew run that actually ran it through the directory's dispatch
sidecar. The legacy id a record naming no review run is filed under is derived
by one public function on this module, so the import script and the store agree
on it by construction. Finally the store accepts records only: a body that is
not a review — one naming a subject but carrying no findings, scores, rubric or
reviewed revision — is refused inside the existing writers.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from reckon.crew import plan_review
from reckon.crew import review as review_module

PROJECT = "store-identity"
REVIEWED_RUN = "r-reviewed-run"
RUN_REVIEW = "r-20261006T120000000000-review-run"
# A review run id that encodes no dispatch instant.
NO_STAMP_RUN = "r-review-run-no-stamp"
PLAN_SLUG = "demo"
PLAN_VERSION = 2

# A plan review's review run id is the composed id its report directory is named
# for — the review reflex mints it from ``plan-review-of-<slug>`` — so it is a
# different id from the crew run that actually ran the review.
COMPOSED_PLAN_REVIEW = f"r-20261006T120000000000-plan-review-of-{PLAN_SLUG}"
CREW_RUN = "r-20261006T120003000000-plan-review-of-demo"

DISPATCH_TS = "2026-10-06T09:00:00+00:00"
COMPLETION_TS = "2026-10-06T09:07:30+00:00"
# The instant ``RUN_REVIEW`` and ``COMPOSED_PLAN_REVIEW`` encode.
ENCODED_TS = "2026-10-06T12:00:00+00:00"
# The instant ``CREW_RUN`` encodes, and the times its own run record carries.
CREW_DISPATCH_TS = "2026-10-06T12:00:03+00:00"
CREW_COMPLETION_TS = "2026-10-06T12:05:00+00:00"


@pytest.fixture()
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synthesised project checkout and config home, isolated from the real ones."""
    monkeypatch.setenv("RECKON_HOME", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    return root


def _run_record(checkout: Path, run_id: str, dispatched: str, completed: str) -> None:
    path = checkout / "docs" / "state" / PROJECT / "runs" / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "project": PROJECT,
                "dispatched_at": dispatched,
                "completed_at": completed,
            }
        ),
        encoding="utf-8",
    )


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _report_directory(tmp_path: Path, run_id: str) -> Path:
    """The plan-review report directory the store resolves the crew run through."""
    return (
        tmp_path
        / "config"
        / "crew"
        / "reports"
        / PROJECT
        / "plan-review"
        / PLAN_SLUG
        / run_id
    )


def _write_dispatch_sidecar(tmp_path: Path, run_id: str, crew_run_id: str) -> None:
    directory = _report_directory(tmp_path, run_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "dispatch.json").write_text(
        json.dumps({"run_id": crew_run_id, "status": "dispatched"}), encoding="utf-8"
    )


# ── Times resolve from the record's own stamps, else the review run id ───────


def test_a_run_review_with_no_run_record_takes_the_encoded_dispatch_time(
    checkout: Path,
) -> None:
    # The review run has no ledger row and no live pointer, and the record
    # carries no stamps of its own: only the dispatch instant the run id encodes
    # resolves, and the completion it cannot supply is left empty rather than
    # defaulted to the store clock.
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": RUN_REVIEW,
        "status": "parsed",
        "scores": {"evidence": 18},
    }
    path = review_module.store_committed_review(record, root=checkout)

    stored = _read(path)
    assert stored[review_module.DISPATCH_TIME_KEY] == ENCODED_TS
    assert stored[review_module.COMPLETION_TIME_KEY] == ""
    assert stored[review_module.TIMES_SOURCE_KEY] == review_module.RUN_ID_TIMES_SOURCE
    assert stored["timestamp"] != ENCODED_TS


def test_a_plan_review_with_no_run_record_takes_its_own_stamps(
    checkout: Path,
) -> None:
    # A plan review's composed run id has no run record; the record carries both
    # stamps, so they are the times used and the run id is not consulted.
    record = {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": COMPOSED_PLAN_REVIEW,
        "dispatched_at": DISPATCH_TS,
        "completed_at": COMPLETION_TS,
        "status": "ready",
        "findings": [],
    }
    path = review_module.store_committed_review(record, root=checkout)

    stored = _read(path)
    assert stored[review_module.DISPATCH_TIME_KEY] == DISPATCH_TS
    assert stored[review_module.COMPLETION_TIME_KEY] == COMPLETION_TS
    assert stored[review_module.TIMES_SOURCE_KEY] == review_module.RECORD_TIMES_SOURCE
    assert stored["timestamp"] not in (DISPATCH_TS, COMPLETION_TS)


def test_a_plan_review_resolves_the_crew_run_its_report_directory_names(
    checkout: Path, tmp_path: Path
) -> None:
    # The composed run id has no run record, so the store reads the report
    # directory's dispatch sidecar, finds the crew run that actually ran the
    # review, and takes both stamps from that run's own committed row.
    _run_record(checkout, CREW_RUN, CREW_DISPATCH_TS, CREW_COMPLETION_TS)
    _write_dispatch_sidecar(tmp_path, COMPOSED_PLAN_REVIEW, CREW_RUN)
    record = {
        "project": PROJECT,
        "plan_slug": PLAN_SLUG,
        "plan_version": PLAN_VERSION,
        "review_run_id": COMPOSED_PLAN_REVIEW,
        "status": "ready",
        "findings": [],
    }
    path = review_module.store_committed_review(record, root=checkout)

    stored = _read(path)
    assert stored[review_module.DISPATCH_TIME_KEY] == CREW_DISPATCH_TS
    assert stored[review_module.COMPLETION_TIME_KEY] == CREW_COMPLETION_TS
    assert (
        stored[review_module.TIMES_SOURCE_KEY] == review_module.RUN_RECORD_TIMES_SOURCE
    )


def test_a_record_resolving_no_time_is_committed_marked_unknown(
    checkout: Path,
) -> None:
    # A record filed under a derived legacy id has no encodable instant in its
    # id, no run record and no stamps of its own, so no source resolves a time.
    # It is still committed, with both stamps left empty and the absence marked
    # under times_source rather than the store clock substituted for a stamp
    # nobody recorded.
    body = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "scores": {"evidence": 18},
    }
    legacy_id = review_module.derived_legacy_review_run_id(
        json.dumps(body, sort_keys=True).encode()
    )
    record = {**body, "review_run_id": legacy_id}
    path = review_module.store_committed_review(record, root=checkout)
    stored = _read(path)

    assert stored[review_module.DISPATCH_TIME_KEY] == ""
    assert stored[review_module.COMPLETION_TIME_KEY] == ""
    assert stored[review_module.TIMES_SOURCE_KEY] == review_module.UNKNOWN_TIMES_SOURCE
    # The store writes its own clock under ``timestamp`` but never substitutes
    # the clock for a stamp, so the empty stamps are not the stored moment.
    assert stored["timestamp"] != ""
    assert stored[review_module.DISPATCH_TIME_KEY] != stored["timestamp"]
    assert stored[review_module.COMPLETION_TIME_KEY] != stored["timestamp"]


def test_a_run_record_with_no_dispatch_stamp_is_committed_unknown(
    checkout: Path,
) -> None:
    # The review run's own record exists yet carries no dispatch stamp, and its
    # id encodes no instant: no source resolves a time. The record is still
    # committed, with both stamps empty and the absence marked under
    # times_source rather than the store clock substituted for a stamp nobody
    # recorded.
    _run_record(checkout, NO_STAMP_RUN, "", "")
    record = {
        "project": PROJECT,
        "reviewed_run_id": REVIEWED_RUN,
        "review_run_id": NO_STAMP_RUN,
        "status": "parsed",
        "scores": {"evidence": 18},
    }
    path = review_module.store_committed_review(record, root=checkout)
    stored = _read(path)

    assert stored[review_module.DISPATCH_TIME_KEY] == ""
    assert stored[review_module.COMPLETION_TIME_KEY] == ""
    assert stored[review_module.TIMES_SOURCE_KEY] == review_module.UNKNOWN_TIMES_SOURCE
    # The store writes its own clock under ``timestamp`` but never substitutes
    # the clock for a stamp, so the store timestamp appears in neither stamp.
    assert stored["timestamp"] != ""
    assert stored[review_module.DISPATCH_TIME_KEY] != stored["timestamp"]
    assert stored[review_module.COMPLETION_TIME_KEY] != stored["timestamp"]


def test_a_record_with_resolvable_times_keeps_them(checkout: Path) -> None:
    # A record whose run id encodes a dispatch instant still resolves that
    # instant, so the unknown marking applies only when nothing resolves.
    dispatched, completed, source = review_module.resolve_record_times(
        PROJECT,
        {"reviewed_run_id": REVIEWED_RUN, "review_run_id": RUN_REVIEW},
        root=checkout,
    )
    assert dispatched == ENCODED_TS
    assert completed == ""
    assert source == review_module.RUN_ID_TIMES_SOURCE


def test_run_id_dispatch_time_parses_the_encoded_instant() -> None:
    assert (
        review_module.run_id_dispatch_time(
            "r-20261006T120000000000-plan-review-of-demo"
        )
        == ENCODED_TS
    )
    # A name carrying no such grammar yields nothing rather than a guess.
    assert review_module.run_id_dispatch_time("r-review-run") == ""
    assert review_module.run_id_dispatch_time("") == ""


# ── One public legacy-id derivation, shared with the import script ───────────


def _load_import_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "import_host_reviews.py"
    spec = importlib.util.spec_from_file_location("import_host_reviews", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_import_script_derives_the_legacy_id_through_the_store() -> None:
    module = _load_import_script()
    raw = b"a record naming no review run"
    body: dict[str, Any] = {
        "project": PROJECT,
        "reviewed_run_id": "r-filename-run",
        "reviewed_head_sha": "a" * 40,
        "findings": [],
    }
    identity = module._review_identity(raw, body, "r-filename-run.json")

    derived = review_module.derived_legacy_review_run_id(raw)
    assert derived.startswith("legacy-")
    assert identity == ("run", "r-filename-run", derived, True)
    # The script carries no second copy of the derivation: the public function
    # on the store is the one owner.
    assert module.review_store is review_module
    assert not hasattr(module, "_derived_review_run_id")
    assert not hasattr(module, "_carries_review_evidence")


def test_the_legacy_id_is_stable_for_one_record_and_distinct_for_two() -> None:
    first = review_module.derived_legacy_review_run_id(b"one record's bytes")
    assert first == review_module.derived_legacy_review_run_id(b"one record's bytes")
    second = review_module.derived_legacy_review_run_id(b"another record's bytes")
    assert first != second


# ── The store accepts records only ───────────────────────────────────────────


def _is_a_review_material(body: dict) -> bool:
    return review_module.carries_review_material(body)


def test_store_review_refuses_a_body_that_is_not_a_review(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="review material"):
        review_module.store_review(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "review_run_id": RUN_REVIEW,
                "status": "parsed",
            },
            base_dir=tmp_path,
        )


def test_store_plan_review_refuses_a_body_that_is_not_a_review(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="review material"):
        plan_review.store_plan_review(
            {
                "project": PROJECT,
                "plan_slug": PLAN_SLUG,
                "plan_version": PLAN_VERSION,
                "review_run_id": RUN_REVIEW,
                "status": "ready",
            },
            base_dir=tmp_path,
        )


def test_store_committed_review_refuses_a_body_that_is_not_a_review(
    checkout: Path, tmp_path: Path
) -> None:
    _run_record(checkout, RUN_REVIEW, DISPATCH_TS, COMPLETION_TS)
    with pytest.raises(ValueError, match="review material"):
        review_module.store_committed_review(
            {
                "project": PROJECT,
                "reviewed_run_id": REVIEWED_RUN,
                "review_run_id": RUN_REVIEW,
                "status": "parsed",
            },
            root=checkout,
        )


def test_review_material_is_named_by_findings_scores_rubric_or_revision() -> None:
    assert _is_a_review_material({"findings": []})
    assert _is_a_review_material({"scores": {}})
    assert _is_a_review_material({"rubric": "design"})
    assert _is_a_review_material({"reviewed_head_sha": "a" * 40})
    assert _is_a_review_material({"reviewed_commit": "a" * 40})
    # A subject alone is not material.
    assert not _is_a_review_material({"reviewed_run_id": REVIEWED_RUN})
    assert not _is_a_review_material({"plan_slug": PLAN_SLUG, "plan_version": 1})
