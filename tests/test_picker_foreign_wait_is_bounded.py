"""A pick serves a published profile and never rebuilds a foreign ledger's.

A pick's expected-wait figure counts every live local worker, including one
from a project whose records the pick did not read, so it needs those projects'
profiles. Building a profile decodes the whole ledger, and a project's profile
is a function of its ledger's contents. Building inside the pick made the pick's
cost grow with the number of live projects -- up to five share the lane here --
and a single synchronous rebuild is what pushed picks past their five-second
dispatch bound. So the pick no longer builds at all: it serves the last profile
a promotion published for the project, or it serves none when there is none fit
to serve.
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


def _groups() -> dict:
    """A profile whose one group's median is 420 s for the live shape."""

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


def test_the_persisted_cache_prefers_reckon_home(tmp_path, monkeypatch):
    """Both cache homes set: the refresh writes under ``RECKON_HOME`` only.

    The suite isolates ``RECKON_HOME``, but a host commonly has
    ``XDG_CACHE_HOME`` pointing at the real user cache. Resolving that first
    would put a fixture's profile in the live cache and, on a second run the
    same day, read it back -- so the home the caller isolated must win, and
    nothing may be written under the other. The write under test is the
    refresh's, since the pick no longer writes a cache entry at all.
    """

    reckon_home = tmp_path / "reckon-home"
    xdg_home = tmp_path / "xdg-home"
    monkeypatch.setenv("RECKON_HOME", str(reckon_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_home))
    monkeypatch.delenv("RECKON_RUN_TIME_PROFILE_CACHE", raising=False)
    monkeypatch.setattr(lane_context, "run_time_profile", lambda _project, **_kw: {})

    lane_context.refresh_run_time_profile("cache-order-probe", now=NOW)

    assert (
        reckon_home / "cache" / "run-time-profile" / "cache-order-probe.json"
    ).is_file()
    assert not xdg_home.exists()


def test_two_picks_serve_a_persisted_foreign_profile(monkeypatch):
    """Four foreign projects, two picks, no ledger read by either pick.

    A profile is published by the refresh (the promotion's own act) and then
    reused. The loader records the project on every call; the refresh calls it
    once per project, and neither pick calls it again, so the pick reads no
    foreign ledger at all. A pick that rebuilt a profile would double every
    count, which this assertion refuses.
    """

    projects = ["foreign-1", "foreign-2", "foreign-3", "foreign-4"]
    monkeypatch.setattr(
        lane_context, "list_live", lambda: [_live_local_row(p) for p in projects]
    )
    reads: list[str] = []

    def profile(project, **_kwargs):
        reads.append(project)
        return _groups()

    monkeypatch.setattr(lane_context, "run_time_profile", profile)

    for project in projects:
        lane_context.refresh_run_time_profile(project, now=NOW)

    for _ in range(2):
        wait, age = lane_context._expected_wait(
            project="picker",
            records=[],
            local_backend="clive",
            now=NOW,
        )
        assert wait == 420.0
        assert age == 0.0

    assert sorted(reads) == sorted(projects)


def test_a_moved_ledger_still_serves_the_persisted_profile(monkeypatch):
    """A foreign project whose ledger moves is not rebuilt by the pick.

    The pick must never build a profile, so a stamp that moves must still serve
    the last published reading rather than force a fresh read. The ledger stamp
    is patched to a new value and the loader is patched to raise, so a pick that
    reached the loader would fail the case; the served profile must stand.
    """

    projects = ["foreign-1", "foreign-2"]
    monkeypatch.setattr(
        lane_context, "list_live", lambda: [_live_local_row(p) for p in projects]
    )
    stamp = {"value": ("first",)}
    monkeypatch.setattr(lane_context, "run_time_profile", lambda _p, **_kw: _groups())
    monkeypatch.setattr(lane_context, "_ledger_stamp", lambda _project: stamp["value"])

    for project in projects:
        lane_context.refresh_run_time_profile(project, now=NOW)

    def refuse(_project, **_kwargs):
        raise AssertionError("the pick rebuilt a profile")

    monkeypatch.setattr(lane_context, "run_time_profile", refuse)
    lane_context._PROFILE_CACHE.clear()

    wait, _age = lane_context._expected_wait(
        project="picker", records=[], local_backend="clive", now=NOW
    )
    assert wait == 420.0

    stamp["value"] = ("second",)
    lane_context._PROFILE_CACHE.clear()
    wait, _age = lane_context._expected_wait(
        project="picker", records=[], local_backend="clive", now=NOW
    )
    assert wait == 420.0


#: A one-shot pick driven in its own interpreter. It puts four foreign
#: projects' live local workers on the lane, replaces the profile loader with a
#: spy that records every project it is asked for, and runs the expected-wait
#: figure once. Each project's profile is published (through the refresh) only
#: when no persisted entry is present yet, standing in for the promotion that
#: publishes it; a pick itself never builds one. Pointed at a shared cache
#: directory, two runs of this script are two separate processes asking for the
#: same profiles.
_DRIVER = """
import json
import os
from datetime import UTC, datetime

from reckon.crew.picker import lane_context

PROJECTS = ["foreign-1", "foreign-2", "foreign-3", "foreign-4"]
SPY = os.environ["PICKER_FOREIGN_WAIT_SPY"]
CACHE = os.environ["RECKON_RUN_TIME_PROFILE_CACHE"]
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

for project in PROJECTS:
    if not os.path.exists(os.path.join(CACHE, project + ".json")):
        lane_context.refresh_run_time_profile(
            project, now=datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
        )

wait, age = lane_context._expected_wait(
    project="picker",
    records=[],
    local_backend="clive",
    now=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
)
print(json.dumps({"expected_wait_s": wait, "expected_wait_profile_age_s": age}))
"""


#: A one-shot pick composed through ``build`` in its own interpreter, with no
#: records handed in, so the pick's own project is served from its persisted
#: profile as well as each live foreign project. It spies every profile it asks
#: for, so two runs against one cache directory show what a fresh dispatch reads.
_BUILD_DRIVER = """
import json
import os
import importlib
from datetime import UTC, datetime
from types import SimpleNamespace

lane_context = importlib.import_module("reckon.crew.picker.lane_context")

PROJECTS = ["own-project", "foreign-1", "foreign-2", "foreign-3", "foreign-4"]
SPY = os.environ["PICKER_FOREIGN_WAIT_SPY"]
CACHE = os.environ["RECKON_RUN_TIME_PROFILE_CACHE"]
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
lane_context.local_lane_load = lambda: {}


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

for project in PROJECTS:
    if not os.path.exists(os.path.join(CACHE, project + ".json")):
        lane_context.refresh_run_time_profile(
            project, now=datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
        )

composed = lane_context.build(
    node=SimpleNamespace(time_budget=""),
    candidates=[],
    project="own-project",
    records=None,
    now=datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
)
print(json.dumps({"expected_wait_s": composed["local_lane"]["expected_wait_s"]}))
"""


def test_a_fresh_process_with_no_records_reads_no_ledger(tmp_path):
    """The pick's own project is served too, so a warm process reads nothing.

    ``build`` with no records once profiled the pick's own project straight from
    its ledger, so a dispatch decoded one ledger on every pick even when nothing
    had changed. Both the own project and each live foreign project now come
    from a persisted profile: the first process publishes all five, and the
    second must build none.
    """

    worktree = Path(__file__).resolve().parents[1]
    driver = tmp_path / "build_driver.py"
    driver.write_text(textwrap.dedent(_BUILD_DRIVER), encoding="utf-8")
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
        "own-project",
    ]
    assert reads[1] == []


def test_reuse_survives_between_processes(tmp_path):
    """Two fresh processes reuse one published reading, so the second builds none.

    A real dispatch is a fresh process, so an in-process memo cannot bound a
    pick: the persisted profile is what carries the reuse between dispatches.
    Each process appends the projects it publishes to its own spy file, and both
    point the cache at one shared directory. The first process publishes all
    four foreign profiles; the second finds them persisted and builds none.
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
