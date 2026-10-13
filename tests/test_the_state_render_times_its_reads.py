"""Every read the picker's state render makes is timed once and named.

The state render reads the published lane document, the live run pointers, the
run-time profile cache and its own template, and some of those reads go to
shared storage. Timing the render as a whole names none of them, so a counter
times each read individually and appends one entry per read beside
``pick-timings.jsonl`` under the crew home. These tests pin that the counter
records one entry per read the render made, each naming its read, and that the
prompt it returns is byte-identical whether the counter is on or off. The
counter only observes: it changes no decision and no rendered prompt.
"""

from __future__ import annotations

import importlib
import json

from reckon.crew.node import TaskNode

picker = importlib.import_module("reckon.crew.picker")
prompts = importlib.import_module("reckon.crew.picker.prompts")
lane_context = importlib.import_module("reckon.crew.picker.lane_context")

#: The reads the state render makes and their names in the counter's log. The
#: first three are read through ``lane_context`` and are stubbed; the template
#: load is read in ``prompts`` and is exercised for real.
STUBBED_READS = ("local_lane_load", "list_live", "_cached_run_time_profile")
TEMPLATE_READ = "template_load"
READS = (*STUBBED_READS, TEMPLATE_READ)


def _node() -> TaskNode:
    return TaskNode(
        id="parser",
        plan="lane-and-model-routing",
        role="implement",
        spec_level="guided",
        goal="Implement a parser",
        done_when="Parser tests pass",
        time_budget="60m",
    )


def _candidate(backend: str = "local") -> picker.Candidate:
    return picker.Candidate(
        backend=backend,
        family="local",
        model="local-model",
        effort="high",
        local=True,
        availability="served",
        utilisation_pct=None,
        burn_multiple=None,
        pace_allowance=None,
        resets_at=None,
        worker_slots=None,
        congestion=None,
        outcomes={},
        reasons=[],
    )


def _stub_reads(monkeypatch):
    """Replace every shared-storage read with a cheap stub, counting calls.

    The counter is timed at the call sites inside ``lane_context``, so the stubs
    stand in for the reads without removing the timing. ``records`` is left None
    on the render so the run-time profile cache is read exactly once.
    """

    calls = dict.fromkeys(STUBBED_READS, 0)

    def local_lane_load():
        calls["local_lane_load"] += 1
        return {}

    def list_live():
        calls["list_live"] += 1
        return []

    def cached_run_time_profile(_project, *, now):
        calls["_cached_run_time_profile"] += 1
        return {"groups": []}

    monkeypatch.setattr(lane_context, "local_lane_load", local_lane_load)
    monkeypatch.setattr(lane_context, "list_live", list_live)
    monkeypatch.setattr(
        lane_context, "_cached_run_time_profile", cached_run_time_profile
    )
    return calls


def _render():
    return prompts.render(
        "state.jinja",
        node=_node(),
        capability={"class": "general"},
        estimated_context=1000,
        estimated_hours=None,
        estimated_hours_source=None,
        comment="keep the API stable",
        candidates=[_candidate()],
        project="example",
        records=None,
        budget_snapshot={},
        config={"default_backend": "local"},
        attempts=1,
    )


def _log_path(home):
    return home / "crew" / lane_context.STATE_READ_TIMINGS_LOG


def _entries(home):
    path = _log_path(home)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _point_home(monkeypatch, home):
    monkeypatch.setenv("RECKON_HOME", str(home))


def test_the_render_times_each_read_once_and_names_it(tmp_path, monkeypatch):
    """One timed entry per read the render made, each naming its read."""
    home = tmp_path / "home"
    _point_home(monkeypatch, home)
    monkeypatch.delenv(lane_context.STATE_READ_TIMINGS_ENV, raising=False)
    calls = _stub_reads(monkeypatch)

    _render()

    assert _log_path(home).parent == (home / "crew")
    entries = _entries(home)
    names = [entry["read"] for entry in entries]
    # Every read the render made carries exactly one entry, and no entry names a
    # read the render did not make: the count matches call for call.
    expected_total = sum(calls.values()) + 1  # the template load, timed in prompts
    assert len(entries) == expected_total
    for read, count in calls.items():
        assert count == 1, read
        assert names.count(read) == count, read
    assert names.count("template_load") == 1
    assert set(names) == set(READS)
    for entry in entries:
        assert isinstance(entry["ms"], (int, float)) and entry["ms"] >= 0
        assert isinstance(entry["at"], str) and entry["at"]


def test_the_prompt_is_byte_identical_with_the_counter_off(tmp_path, monkeypatch):
    """Turning the counter on leaves the rendered prompt unchanged."""
    home = tmp_path / "home"
    _point_home(monkeypatch, home)
    monkeypatch.delenv(lane_context.STATE_READ_TIMINGS_ENV, raising=False)
    _stub_reads(monkeypatch)

    with_counter_on = _render()
    on_entries = _entries(home)
    assert [entry["read"] for entry in on_entries]  # the counter wrote its reads

    monkeypatch.setenv(lane_context.STATE_READ_TIMINGS_ENV, "0")
    _stub_reads(monkeypatch)
    with_counter_ok = _render()

    assert with_counter_ok == with_counter_on
    assert _entries(home) == on_entries  # the off render appended nothing
