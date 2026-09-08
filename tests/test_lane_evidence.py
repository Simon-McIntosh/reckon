from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

import pytest

from reckon import flight
from reckon.crew.lane_evidence import MINIMUM_LANE_SAMPLE, lane_evidence
from reckon.crew.quota_weight import RequestTokenUsage, quota_weight


@pytest.fixture(autouse=True)
def configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the test model and its rates through the flight configuration."""
    config = {
        "backends": {
            "fixture-lane": {
                "model": "fixture-model",
                "input_rate_per_million": 4.00,
                "output_rate_per_million": 20.00,
            },
        }
    }
    monkeypatch.setattr(flight, "resolve", lambda: SimpleNamespace(config=config))


def _configured_model_identifier() -> str:
    return next(iter(flight.resolve().config["backends"].values()))["model"]


def _run(
    *,
    lane: str,
    node: str,
    dispute_count: int | str = 0,
    wall_seconds: int | None = 100,
    requests: list[dict[str, int]] | None = None,
    role: str = "implement",
    spec_level: str = "exact",
    durable: bool = True,
) -> dict[str, object]:
    row: dict[str, object] = {
        "backend": lane,
        "node": node,
        "role": role,
        "spec_level": spec_level,
        "dispute_count": dispute_count,
        "wall_seconds": wall_seconds,
        "commits": [f"{lane}-{node}"] if durable else [],
        "agent": {"model": _configured_model_identifier()},
        "gate": "passed",
    }
    if requests is not None:
        row["quota_requests"] = requests
    return row


def _lane(result: dict[str, object], name: str) -> dict[str, object]:
    rows = result["lanes"]
    assert isinstance(rows, list)
    return next(row for row in rows if row["lane"] == name)


def _metric(row: dict[str, object], name: str) -> dict[str, object]:
    metric = row[name]
    assert isinstance(metric, dict)
    return metric


def _requests(input_tokens: int, output_tokens: int) -> list[dict[str, int]]:
    return [{"input_tokens": input_tokens, "output_tokens": output_tokens}]


def _weights(rows: Iterable[dict[str, object]]) -> float:
    total = 0.0
    for row in rows:
        requests = row["quota_requests"]
        assert isinstance(requests, list)
        usage = [RequestTokenUsage(**request) for request in requests]
        agent = row["agent"]
        assert isinstance(agent, dict)
        result = quota_weight(agent["model"], usage)
        total += result.weight
    return total


def test_locked_discriminators_distinguish_identical_gate_and_rework_outcomes() -> None:
    rows = [
        *[
            _run(
                lane="premise-aware",
                node=f"aware-{index}",
                dispute_count=index % 2,
                wall_seconds=100 + index,
                requests=_requests(10 + index, 1),
            )
            for index in range(MINIMUM_LANE_SAMPLE)
        ],
        *[
            _run(
                lane="premise-silent",
                node=f"silent-{index}",
                dispute_count=0,
                wall_seconds=200 + index,
                requests=_requests(20 + index, 2),
            )
            for index in range(MINIMUM_LANE_SAMPLE)
        ],
    ]

    result = lane_evidence(rows, "implement", "exact")
    aware = _lane(result, "premise-aware")
    silent = _lane(result, "premise-silent")

    expected_dispute = (
        sum(row["dispute_count"] for row in rows[:MINIMUM_LANE_SAMPLE])
        / MINIMUM_LANE_SAMPLE
    )
    expected_wall = (
        sum(row["wall_seconds"] for row in rows[:MINIMUM_LANE_SAMPLE])
        / MINIMUM_LANE_SAMPLE
    )
    expected_cost = _weights(rows[:MINIMUM_LANE_SAMPLE]) / MINIMUM_LANE_SAMPLE
    assert _metric(aware, "dispute_count_per_attempt")["value"] == expected_dispute
    assert _metric(aware, "wall_seconds_per_durable_node")["value"] == expected_wall
    assert _metric(aware, "cost_per_durable_node")["value"] == expected_cost
    assert (
        _metric(aware, "dispute_count_per_attempt")["value"]
        != _metric(silent, "dispute_count_per_attempt")["value"]
    )
    assert (
        _metric(aware, "wall_seconds_per_durable_node")["value"]
        != _metric(silent, "wall_seconds_per_durable_node")["value"]
    )
    assert (
        _metric(aware, "cost_per_durable_node")["value"]
        != _metric(silent, "cost_per_durable_node")["value"]
    )


def test_disputes_are_aggregated_per_attempt_not_deduplicated_node() -> None:
    repeated = [
        _run(
            lane="resumed",
            node="one-node",
            dispute_count=2,
            requests=_requests(10, 1),
        )
        for _ in range(MINIMUM_LANE_SAMPLE - 1)
    ]
    rows = [
        *repeated,
        _run(lane="resumed", node="other-node", requests=_requests(10, 1)),
    ]

    result = lane_evidence(rows, "implement", "exact")
    metric = _metric(_lane(result, "resumed"), "dispute_count_per_attempt")
    expected = sum(row["dispute_count"] for row in rows) / len(rows)
    assert metric["value"] == expected
    assert metric["value"] != sum(
        {row["node"]: row["dispute_count"] for row in rows}.values()
    ) / len({row["node"] for row in rows})


def test_missing_token_measurement_is_never_a_zero_cost() -> None:
    measured = [
        _run(lane="metered", node=str(index), requests=_requests(index + 1, index))
        for index in range(MINIMUM_LANE_SAMPLE)
    ]
    missing = [
        _run(lane="unmetered", node=str(index), requests=None)
        for index in range(MINIMUM_LANE_SAMPLE)
    ]

    result = lane_evidence([*measured, *missing], "implement", "exact")
    known = _metric(_lane(result, "metered"), "cost_per_durable_node")
    unknown = _metric(_lane(result, "unmetered"), "cost_per_durable_node")
    assert known["value"] == _weights(measured) / len(measured)
    assert unknown == {
        "state": "unmeasured",
        "sample_size": len(missing),
        "value": None,
    }
    assert unknown["value"] != known["value"]


def test_sample_floor_shape_filter_and_empty_population_are_explicit() -> None:
    below_floor = [
        _run(lane="small", node=str(index), requests=_requests(1, 1))
        for index in range(MINIMUM_LANE_SAMPLE - 1)
    ]
    at_floor = [
        _run(lane="enough", node=str(index), requests=_requests(1, 1))
        for index in range(MINIMUM_LANE_SAMPLE)
    ]
    other_shape = [
        _run(
            lane="other-shape",
            node=str(index),
            role="review",
            spec_level="guided",
            requests=_requests(100, 1),
        )
        for index in range(MINIMUM_LANE_SAMPLE)
    ]

    result = lane_evidence(
        [*below_floor, *at_floor, *other_shape], "implement", "exact"
    )
    assert result["shape"] == {"role": "implement", "spec_level": "exact"}
    assert {row["lane"] for row in result["lanes"]} == {"small", "enough"}
    small = _lane(result, "small")
    enough = _lane(result, "enough")
    assert small["sample_size"] == len(below_floor)
    assert small["state"] == "insufficient_evidence"
    assert _metric(small, "dispute_count_per_attempt")["sample_size"] == len(
        below_floor
    )
    assert enough["state"] == "measured"
    assert _metric(enough, "dispute_count_per_attempt")["state"] == "measured"

    empty = lane_evidence([], "implement", "exact")
    assert empty["lanes"] == [
        {
            "lane": None,
            "sample_size": 0,
            "state": "insufficient_evidence",
            "minimum_sample_size": MINIMUM_LANE_SAMPLE,
            "dispute_count_per_attempt": {
                "state": "insufficient_evidence",
                "sample_size": 0,
                "value": None,
            },
            "cost_per_durable_node": {
                "state": "insufficient_evidence",
                "sample_size": 0,
                "value": None,
            },
            "wall_seconds_per_durable_node": {
                "state": "insufficient_evidence",
                "sample_size": 0,
                "value": None,
            },
        }
    ]
