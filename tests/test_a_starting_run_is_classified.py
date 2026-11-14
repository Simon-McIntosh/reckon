"""A run its launcher has claimed but not started is classified, not raised on.

A pointer in a pre-spawn phase keeps its phase reading and skips the liveness
arms, but no branch set its classification, so on 14 November one ``starting``
pointer made ``classify_pointer`` raise ``UnboundLocalError`` and took the
project's whole live view down with it.
"""

from __future__ import annotations

import pytest

from reckon.crew import recovery


@pytest.mark.parametrize("phase", ["starting", "launching", "dispatching"])
def test_a_pre_spawn_run_reads_as_running(home, phase):
    record = {
        "run_id": f"r-20261114T201401429252-{phase}",
        "project": "proj",
        "session": "s-1",
        "phase": phase,
        "launch": "cli",
        "node": {"id": phase},
    }
    verdict = recovery.classify_pointer(record)
    assert verdict["classification"] == "running"
    assert verdict["run_id"] == record["run_id"]
