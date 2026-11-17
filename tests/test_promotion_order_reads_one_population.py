"""Promotion ordering resolves every review from one pass-local population."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reckon.crew import promotion_requests as requests
from reckon.crew import recovery_review_delivery as delivery
from reckon.crew import runs

NOW = datetime(2026, 11, 16, 22, tzinfo=UTC)
PROJECT = "pointer-order-fixture"


def _population(root: Path) -> tuple[list[dict], dict[str, str]]:
    live = root / "live"
    queued = root / "promotion-requests"
    live.mkdir(parents=True)
    queued.mkdir()
    pointers = [
        {
            "run_id": f"r-{index:03}",
            "project": PROJECT if index >= 100 else "other-project",
            "node": {"id": f"source-{index % 100:03}"},
        }
        for index in range(300)
    ]
    # Two same-project matches preserve the first match in listing order.
    pointers[121]["node"]["id"] = "ambiguous-source"
    pointers[221]["node"]["id"] = "ambiguous-source"
    for pointer in pointers:
        (live / f"{pointer['run_id']}.json").write_text(json.dumps(pointer))
    rows = []
    identities = {}
    for index in range(100):
        run_id = f"r-{100 + index:03}" if index < 20 else f"r-request-{index:03}"
        node = f"delivery-{index}"
        if 20 <= index < 40:
            node = f"review-of-source-{index - 20:03}"
            identities[run_id] = f"r-{100 + index - 20:03}"
        elif 40 <= index < 45:
            node = f"review-of-r-{100 + index - 40:03}"
            identities[run_id] = f"r-{100 + index - 40:03}"
        elif index == 45:
            node = "review-of-ambiguous-source"
            identities[run_id] = "r-121"
        rows.append(
            {
                "run_id": run_id,
                "node": node,
                "project": PROJECT,
                "phase": requests.REQUESTED,
                "session": "one-session",
                "priority_override": 5 if index % 7 == 0 else None,
                "requested_at": (NOW - timedelta(seconds=100 - index)).isoformat(),
            }
        )
    for row in rows:
        (queued / f"{row['run_id']}.json").write_text(json.dumps(row))
    return rows, identities


@pytest.fixture
def population(tmp_path, monkeypatch):
    rows, identities = _population(tmp_path)
    monkeypatch.setattr(runs, "crew_home", lambda: tmp_path)
    monkeypatch.setattr(runs, "live_dir", lambda: tmp_path / "live")
    # Ordering is read-only: publishing watch transitions is outside this read.
    calls = []

    def list_live(**kwargs):
        calls.append(kwargs)
        return runs._list_live_records(**kwargs)

    monkeypatch.setattr(runs, "list_live", list_live)
    monkeypatch.setattr(delivery, "list_live", list_live)
    return rows, identities, calls


def test_a_mixed_queue_reads_one_population_and_preserves_order(
    population, monkeypatch
):
    rows, identities, calls = population
    observed = {}
    resolve = requests._reviewed_run_id

    def record_identity(request, **kwargs):
        reviewed = resolve(request, **kwargs)
        observed[request["run_id"]] = reviewed
        return reviewed

    monkeypatch.setattr(requests, "_reviewed_run_id", record_identity)
    waiting = {row["run_id"] for row in rows}
    review_first = {key for key, value in identities.items() if value in waiting}
    expected = sorted(
        rows,
        key=lambda row: (
            0
            if row["run_id"] in review_first
            else 1
            if row["priority_override"]
            else 2,
            row["requested_at"],
            row["run_id"],
        ),
    )

    ordered = requests._pending_requests(now=NOW)

    assert [row["run_id"] for row in ordered] == [row["run_id"] for row in expected]
    print(f"requests={len(ordered)} pointers=300 listings={len(calls)}")
    assert len(calls) <= 1, f"source-node cohort made {len(calls)} listings"
    assert observed == {
        row["run_id"]: identities.get(row["run_id"], "") for row in rows
    }


@pytest.mark.parametrize("reviews", [False, True])
def test_a_pass_without_source_node_reviews_needs_no_listing(population, reviews):
    rows, _, calls = population
    for row in rows:
        keep = (
            row["node"].startswith("review-of-r-")
            if reviews
            else not row["node"].startswith("review-of-")
        )
        if not keep:
            requests._request_path(row["run_id"]).unlink()

    ordered = requests._pending_requests(now=NOW)

    assert len(ordered) == (5 if reviews else 74)
    assert calls == []


def test_a_new_pass_reads_changed_pointer_identities(population):
    _, _, calls = population
    requests._pending_requests(now=NOW)
    path = runs.live_dir() / "r-100.json"
    pointer = json.loads(path.read_text())
    pointer["node"]["id"] = "retired-source"
    path.write_text(json.dumps(pointer))

    ordered = requests._pending_requests(now=NOW)

    assert len(calls) == 2
    review = next(row for row in ordered if row["run_id"] == "r-request-020")
    assert review["ordering_reason"] != "delivered review"


def test_deferral_shares_one_listing_across_unrelated_reviews(population):
    rows, _, calls = population
    target = {**rows[0], "run_id": "r-no-reviewed-match", "request_id": "request"}
    refusal = (
        "run 'r-no-reviewed-match' is classified scoring; no complete independent "
        "review is stored. Produce it with `reckon crew dispatch --review`"
    )

    assert requests._defer_to_a_queued_review(target, refusal) is False
    assert len(calls) <= 1


def test_a_supplied_empty_population_never_relists(population, monkeypatch):
    _, _, calls = population
    monkeypatch.setattr(
        delivery, "_durable_reviewed_run_id", lambda *args: "source-000"
    )
    assert (
        delivery._resolved_reviewed_run_id(
            {"node": {"id": "review-of-source-000"}}, PROJECT, pointers=[]
        )
        == "source-000"
    )
    assert calls == []


def test_an_empty_population_is_shared_across_the_pass(population, monkeypatch):
    _, _, calls = population
    monkeypatch.setattr(runs, "_list_live_records", lambda **kwargs: [])
    monkeypatch.setattr(
        delivery, "_durable_reviewed_run_id", lambda node, source, project: source
    )

    assert len(requests._pending_requests(now=NOW)) == 100
    assert len(calls) == 1


def test_an_unreadable_population_does_not_stop_ordering(population, monkeypatch):
    _, _, calls = population

    def unreadable(**kwargs):
        calls.append(kwargs)
        raise OSError("pointer directory cannot be read")

    monkeypatch.setattr(runs, "list_live", unreadable)
    monkeypatch.setattr(delivery, "list_live", unreadable)

    ordered = requests._pending_requests(now=NOW)

    assert len(ordered) == 100
    assert len(calls) == 1
    assert sum(row["ordering_reason"] == "delivered review" for row in ordered) == 5
