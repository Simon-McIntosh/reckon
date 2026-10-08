"""The picker's budget view is cached on its input files, and ages on every call.

A budget view composes recorded windows, the published paid-lanes document and
budget preflight, which together read the ledger, the paid-lanes document, the
banked-reset record and the lane documents. Against live state that is a few
hundred milliseconds a dispatch paid to rebuild a figure the same files had
already produced. The view is now cached on the stamps of those files, so an
unchanged set reuses it, and every time-derived field is recomputed on each
call so a cached entry never serves a stale age.
"""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime, timedelta

import pytest

from reckon.crew.picker import snapshot


@pytest.fixture
def budget_config() -> dict:
    return {
        "backends": {
            "local": {
                "model": "local-model",
                "launch": "cli",
                "lane_document": None,
            }
        },
        "local_backend": "local",
    }


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    root = tmp_path / "pick-cache"
    monkeypatch.setenv("RECKON_PICK_CACHE", str(root))
    return root


def _call(project, config, repo, records, *, now, cache_root):
    return snapshot.budget_view(
        project, config, repo, records, cached_only=True, now=now, cache_root=cache_root
    )


def test_cached_and_uncached_results_are_identical(
    tmp_path, budget_config, cache_root, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    records: list[dict] = []
    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    first = _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    # A fresh cache location forces the uncached composition at the same moment.
    second = _call(
        "sample",
        budget_config,
        repo,
        records,
        now=moment,
        cache_root=cache_root / "other",
    )
    assert first == second


def test_a_second_call_rebuilds_nothing(
    tmp_path, budget_config, cache_root, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    records: list[dict] = []
    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    calls: list[int] = []
    real_preflight = snapshot.budget.preflight

    def counting_preflight(*args, **kwargs):
        calls.append(1)
        return real_preflight(*args, **kwargs)

    monkeypatch.setattr(snapshot.budget, "preflight", counting_preflight)

    open_paths: list[str] = []
    real_open = builtins.open

    def counting_open(file, *args, **kwargs):
        open_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    assert calls, "the first call must compose the view"

    monkeypatch.setattr(builtins, "open", counting_open)
    open_paths.clear()
    _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)

    assert len(calls) == 1, "a hit must not rebuild the view"
    paid_lanes = str(snapshot.paid_lanes.document_path())
    assert paid_lanes not in open_paths, f"a hit re-read the paid-lanes document: {open_paths}"


def test_touching_the_paid_lanes_document_invalidates_the_cache(
    tmp_path, budget_config, cache_root, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    records: list[dict] = []
    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    document = tmp_path / "paid-lanes.json"
    document.write_text(json.dumps({"streams": {}}))
    monkeypatch.setattr(snapshot.paid_lanes, "document_path", lambda path=None: document)

    calls: list[int] = []
    real_preflight = snapshot.budget.preflight

    def counting_preflight(*args, **kwargs):
        calls.append(1)
        return real_preflight(*args, **kwargs)

    monkeypatch.setattr(snapshot.budget, "preflight", counting_preflight)

    _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    assert len(calls) == 1, "the first call must compose the view"
    _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    assert len(calls) == 1, "an unchanged set must reuse the cached view"

    document.write_text(json.dumps({"streams": {}, "touched": "1"}))
    _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    assert len(calls) == 2, "a stamped change to a read file must invalidate the cache"


def test_advancing_the_clock_changes_ages_but_not_file_derived_fields(
    tmp_path, budget_config, cache_root, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    records: list[dict] = []
    moment = datetime(2026, 10, 8, 0, 0, tzinfo=UTC)

    first = _call("sample", budget_config, repo, records, now=moment, cache_root=cache_root)
    later = _call(
        "sample",
        budget_config,
        repo,
        records,
        now=moment + timedelta(hours=3),
        cache_root=cache_root,
    )

    assert later["checked_at"] != first["checked_at"]
    for before_group, after_group in zip(first["groups"], later["groups"]):
        for clock, before_clock in before_group["clocks"].items():
            after_clock = after_group["clocks"].get(clock)
            if after_clock and before_clock.get("age_seconds") is not None:
                assert after_clock["age_seconds"] == pytest.approx(
                    before_clock["age_seconds"] + 3 * 3600
                )
        if before_group["allowance"].get("elapsed_hours") is not None:
            assert after_group["allowance"]["elapsed_hours"] == pytest.approx(
                before_group["allowance"]["elapsed_hours"] + 3.0
            )
    assert first["policy"] == later["policy"]