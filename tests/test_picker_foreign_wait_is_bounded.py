"""Bound a pick's cost by the number of live foreign projects.

A pick's expected-wait figure counts every live local worker, including one
from a project whose records the pick did not read, so it must profile those
projects. Profiling a project decodes its whole ledger. A pick that re-reads
every foreign project's ledger on every pass therefore grows with the number of
projects live on the local lane -- up to five share it here -- and that cost is
what pushes a pick past its five-second dispatch bound. A project's profile is a
function of its ledger's contents, so a reading whose ledger has not moved is
the same answer and is reused across picks rather than taken again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reckon.crew.picker import lane_context

NOW = datetime(2026, 10, 3, 4, 0, tzinfo=UTC)


def _live_local_row(project: str) -> dict:
    """One live local worker belonging to ``project``."""

    return {
        "project": project,
        "backend": "clive",
        "role": "implement",
        "spec_level": "guided",
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }


@pytest.fixture(autouse=True)
def _empty_profile_cache():
    """Each case starts and ends with no memoized profile.

    The reuse under test lives in module state shared by every caller in the
    process, so a profile a prior case left behind would let this one pass
    without exercising the reuse it means to pin.
    """

    lane_context._PROFILE_CACHE.clear()
    yield
    lane_context._PROFILE_CACHE.clear()


def test_two_picks_read_each_foreign_ledger_once(monkeypatch):
    """Four foreign projects, two picks, one ledger read each.

    Nothing about any ledger changes between the picks, so each project's
    profile is unchanged and is reused. The loader records the project on every
    call; a pick that re-reads each foreign ledger doubles every count on the
    second pass, which this assertion refuses.
    """

    projects = ["foreign-1", "foreign-2", "foreign-3", "foreign-4"]
    monkeypatch.setattr(
        lane_context, "list_live", lambda: [_live_local_row(p) for p in projects]
    )
    reads: list[str] = []

    def profile(project, **_kwargs):
        reads.append(project)
        return {
            "groups": [
                {
                    "backend": "clive",
                    "effort": "standard",
                    "role": "implement",
                    "spec_level": "guided",
                    "runs": 3,
                    "wall_seconds_median": 420.0,
                }
            ]
        }

    monkeypatch.setattr(lane_context, "run_time_profile", profile)

    for _ in range(2):
        wait = lane_context._expected_wait(
            project="picker",
            records=[],
            local_backend="clive",
            now=NOW,
        )
        assert wait == 420.0

    assert sorted(reads) == sorted(projects)


def test_a_moved_ledger_is_read_against(monkeypatch):
    """A foreign project whose ledger changes is profiled again.

    Reuse must not be a blind cache: the wait figure is only as good as the
    ledger it was read from, so a stamp that moves has to force a fresh read.
    The ledger stamp is patched to return a new value on the third pick and the
    loader must be asked again for each project.
    """

    projects = ["foreign-1", "foreign-2"]
    monkeypatch.setattr(
        lane_context, "list_live", lambda: [_live_local_row(p) for p in projects]
    )
    reads: list[str] = []
    stamp = {"value": ("first",)}

    def profile(project, **_kwargs):
        reads.append(project)
        return {
            "groups": [
                {
                    "backend": "clive",
                    "effort": "standard",
                    "role": "implement",
                    "spec_level": "guided",
                    "runs": 3,
                    "wall_seconds_median": 420.0,
                }
            ]
        }

    monkeypatch.setattr(lane_context, "run_time_profile", profile)
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _project: stamp["value"])

    lane_context._expected_wait(
        project="picker", records=[], local_backend="clive", now=NOW
    )
    assert sorted(reads) == sorted(projects)

    stamp["value"] = ("second",)
    lane_context._expected_wait(
        project="picker", records=[], local_backend="clive", now=NOW
    )
    assert sorted(reads) == sorted(projects * 2)


#: A one-shot pick driven in its own interpreter. It puts four foreign
#: projects' live local workers on the lane, replaces the profile loader with a
#: spy that records every project it is asked for, and runs the expected-wait
#: figure once. Pointed at a shared cache directory, two runs of this script are
#: two separate processes asking for the same profiles.
_DRIVER = """
import json
import os
from datetime import UTC, datetime

from reckon.crew.picker import lane_context

PROJECTS = ["foreign-1", "foreign-2", "foreign-3", "foreign-4"]
SPY = os.environ["PICKER_FOREIGN_WAIT_SPY"]
# Create the spy file up front, so "no reads" is an empty file this process
# made rather than a file its absence left missing.
open(SPY, "a", encoding="utf-8").close()

lane_context.list_live = lambda: [
    {
        "project": project,
        "backend": "clive",
        "role": "implement",
        "spec_level": "guided",
        "phase": "working",
        "agent": {"local": True, "effort": "standard"},
        "node": {"role": "implement", "spec_level": "guided"},
    }
    for project in PROJECTS
]


def spy(project, **_kwargs):
    with open(SPY, "a", encoding="utf-8") as handle:
        handle.write(project + "\\n")
    return {
        "groups": [
            {
                "backend": "clive",
                "effort": "standard",
                "role": "implement",
                "spec_level": "guided",
                "runs": 3,
                "wall_seconds_median": 420.0,
            }
        ]
    }


lane_context.run_time_profile = spy
wait = lane_context._expected_wait(
    project="picker",
    records=[],
    local_backend="clive",
    now=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
)
print(json.dumps({"expected_wait_s": wait}))
"""


def test_reuse_survives_between_processes(tmp_path):
    """Two fresh processes reuse one reading, so the second reads no ledger.

    A real dispatch is a fresh process, so an in-process memo cannot bound a
    pick: the persisted profile is what carries the reuse between dispatches.
    Each process appends the projects it profiles to its own spy file, and both
    point the cache at one shared directory. The first process reads all four
    foreign ledgers; the second must read none, its figure coming from the
    persisted profile.
    """

    worktree = Path(__file__).resolve().parents[1]
    driver = tmp_path / "pick_driver.py"
    driver.write_text(textwrap.dedent(_DRIVER), encoding="utf-8")
    cache = tmp_path / "profile-cache"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree)
    env["RECKON_RUN_TIME_PROFILE_CACHE"] = str(cache)

    reads: list[list[str]] = []
    for name in ("first-spy.txt", "second-spy.txt"):
        spy = tmp_path / name
        env["PICKER_FOREIGN_WAIT_SPY"] = str(spy)
        completed = subprocess.run(
            [sys.executable, str(driver)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        payload = json.loads(completed.stdout)
        assert payload["expected_wait_s"] == 420.0
        reads.append(spy.read_text(encoding="utf-8").split())

    assert sorted(reads[0]) == [
        "foreign-1",
        "foreign-2",
        "foreign-3",
        "foreign-4",
    ]
    assert reads[1] == []
