"""The local drainer keeps admitting every route when the local harness changes.

One admission service is declared for the workstation, named for a local
backend, and it is the drainer that admits every route: a request queued under
any other backend launches only through it. Two local backends can sit in front
of one model server, each a different worker harness publishing the same lane
document. When the configured local backend moves from one to the other, a
drainer compared by name stops being "the local drainer" and admits only the
requests queued under its own backend name; every other queued request waits
with no pass that will ever launch it.

Each case reads the decision the pass's lane filter makes, over a configuration
built in the case's own directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reckon.crew.admission import selection


def _config(tmp_path: Path, *, local: str) -> dict:
    lane = tmp_path / "lane.json"
    lane.write_text(json.dumps({"headroom": 4}), encoding="utf-8")
    other = tmp_path / "metered.json"
    other.write_text(json.dumps({"headroom": 1}), encoding="utf-8")
    return {
        "local_backend": local,
        "backends": {
            "harness-a": {"lane_document": str(lane)},
            "harness-b": {"lane_document": str(lane)},
            "metered": {"lane_document": str(other)},
            "undeclared": {},
        },
    }


def test_a_drainer_named_for_the_configured_backend_serves_the_local_lane(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, local="harness-a")

    assert selection.drainer_serves_the_local_lane("harness-a", config)


def test_a_drainer_named_for_the_other_harness_still_serves_the_local_lane(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, local="harness-b")

    assert selection.drainer_serves_the_local_lane("harness-a", config)


def test_one_lane_document_spelled_two_ways_is_one_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _config(tmp_path, local="harness-b")
    config["backends"]["harness-b"]["lane_document"] = "~/lane.json"

    assert selection.drainer_serves_the_local_lane("harness-a", config)


@pytest.mark.parametrize("lane", ["metered", "undeclared", "absent", ""])
def test_a_drainer_on_another_lane_is_not_the_local_drainer(
    tmp_path: Path, lane: str
) -> None:
    config = _config(tmp_path, local="harness-b")

    assert not selection.drainer_serves_the_local_lane(lane, config)


def test_two_backends_declaring_no_lane_document_are_not_one_lane(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, local="undeclared")
    config["backends"]["also-undeclared"] = {}

    assert not selection.drainer_serves_the_local_lane("also-undeclared", config)
