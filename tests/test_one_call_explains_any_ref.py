"""One read explains any ref: its state, its waits, its holders, its events.

``read_plan(view="why", ...)`` answers the question a coordinator otherwise settles
by reading a plan, then a pointer file, then messaging a peer. The fixture here
is the shape that question takes: section ``s1`` of ``alpha`` waits on
``beta#s1``, ``beta#s1`` waits on ``gamma#s1``, a run is working on ``beta#s1``
and holding a path, and an open followup names ``alpha`` §1.

The answer is composed from the owners that already hold each fact — the
roadmap's section edges and live-run partition, and the plan's own timestamped
records — so a wait, a run or an event here cannot disagree with the roadmap, the
fleet surface or the plan. The repository, the mounted docs tree and the crew
home are synthesised under the test's own ``tmp_path``, so nothing outside them
is read or written.
"""

from __future__ import annotations

import json
from html import escape
from pathlib import Path

import pytest

import reckon.mcp as mcp_module
from reckon.crew import runs
from reckon.live_run_classification import (
    DECISION_BLOCKER,
    GATE_BLOCKER,
    SECTION_DEPENDENCY_BLOCKER,
)
from reckon.live_run_classification import HELD_PATH as HELD_PATH_KIND
from reckon.mcp_budget import VIEW_BUDGETS, serialised_characters
from tests.mcp_family_reload import reload_mcp_family

PROJECT = "alpha"
WAITING_PLAN = "alpha"
WAITING_SECTION = "s1"
HELD_PLAN = "beta"
HELD_SECTION = "s1"
DOWNSTREAM_PLAN = "gamma"
DOWNSTREAM_SECTION = "s1"
RUN_ID = "r-fixture-why"
SESSION = "coordinator-session"
HELD_PATH = "reckon/held.py"
WRITTEN_AT = "2026-09-01T00:05:00Z"


# ── fixture ─────────────────────────────────────────────────────────────────


def _meta(name: str, value: str) -> str:
    return f'<meta name="{name}" content="{escape(value, quote=True)}">'


def _write_plan(
    docs: Path,
    slug: str,
    *,
    sections: dict[str, str],
    section_depends_on: dict[str, list[str]] | None = None,
    followups: list[dict[str, str]] | None = None,
    decisions: dict[str, list[str]] | None = None,
    gates: list[str] | None = None,
) -> Path:
    """One in-progress plan with the given declarations and authored waits."""

    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        _meta("plan-slug", slug),
        _meta("plan-title", slug),
        '<meta name="plan-status" content="active">',
        '<meta name="plan-effort" content="M">',
        _meta(
            "plan-section-declarations",
            json.dumps(sections, separators=(",", ":")),
        ),
    ]
    if section_depends_on:
        head.append(
            _meta(
                "plan-section-depends-on",
                json.dumps(section_depends_on, separators=(",", ":")),
            )
        )
    head.append(f"<title>{slug}</title></head>")
    body = ['<body><main class="plan-doc">']
    body.extend(
        (
            f'<h2 id="{name}">Section</h2>'
            '<section data-reckon="section"'
            f' data-id="{name}"'
            ' data-effort-hours="2"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard"'
            ' data-capability-risk="low"'
            f' data-status="{declaration}" data-links=""></section>'
        )
        for name, declaration in sorted(sections.items())
    )
    if decisions:
        body.append('<section data-reckon="decisions" id="decisions">')
        body.extend(
            (
                f'<div class="r-dec" data-key="{key}"'
                f' data-sections="{",".join(sections)}">'
                f'<p class="r-dec-q">Which way for {key}?</p></div>'
            )
            for key, sections in sorted(decisions.items())
        )
        body.append("</section>")
    if gates:
        body.append('<section data-reckon="gates" id="gates" class="r-gates">')
        body.extend(
            (
                f'<div class="r-gate" data-id="{gate}"'
                f' data-gated-sections="{WAITING_SECTION}"'
                ' data-status="pending" data-verdict="">'
                f'<h4 class="r-gate-measure">{gate} measure</h4></div>'
            )
            for gate in gates
        )
        body.append("</section>")
    if followups:
        body.append('<section data-reckon="followups" id="followups">')
        body.extend(
            (
                f'<article class="r-fu" data-id="{followup["id"]}"'
                f' data-status="{followup.get("status", "open")}"'
                f' data-written-at="{followup.get("written_at", "")}">'
                f'<p class="r-fu-title">{followup.get("title", "")}</p>'
                f'<p class="r-fu-prompt">{followup.get("prompt", "")}</p>'
                "</article>"
            )
            for followup in followups
        )
        body.append("</section>")
    body.append("</main></body></html>")
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(head) + "".join(body), encoding="utf-8")
    return path


def _running_pointer() -> dict:
    """A pointer whose worker reads alive on another host, so it stays in flight."""

    return {
        "run_id": RUN_ID,
        "project": PROJECT,
        "member": "worker-one",
        "session_id": SESSION,
        "created_at": "2026-09-01T00:00:00Z",
        "phase": "working",
        "process_alive": True,
        "launcher_host": "another-login-node",
        "node": {
            "id": "a-node",
            "plan": HELD_PLAN,
            "section": HELD_SECTION,
            "write_paths": [HELD_PATH],
        },
    }


@pytest.fixture()
def why_fixture(tmp_path: Path, isolated_reckon_home: Path, monkeypatch):
    """Section A waits on B, B is held by a running run, a followup on A is open."""

    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    reload_mcp_family()

    _write_plan(
        docs,
        WAITING_PLAN,
        sections={WAITING_SECTION: "implementable"},
        section_depends_on={WAITING_SECTION: [f"{HELD_PLAN}#{HELD_SECTION}"]},
        followups=[
            {
                "id": "next",
                "status": "open",
                "title": "explain the wait before dispatching",
                "written_at": WRITTEN_AT,
                "prompt": f"/reckon-build {WAITING_PLAN} §1",
            }
        ],
    )
    _write_plan(
        docs,
        HELD_PLAN,
        sections={HELD_SECTION: "implementable"},
        section_depends_on={HELD_SECTION: [f"{DOWNSTREAM_PLAN}#{DOWNSTREAM_SECTION}"]},
    )
    _write_plan(docs, DOWNSTREAM_PLAN, sections={DOWNSTREAM_SECTION: "implementable"})

    runs._write_json(runs.pointer_path(RUN_ID), _running_pointer())
    return docs


def _why(slug: str, section: str | None = None) -> dict:
    return mcp_module._read_plan_tool(
        project=PROJECT,
        slug=slug,
        section=section,
        view="why",
    )


# ── the answer ──────────────────────────────────────────────────────────────


def test_the_read_explains_a_section_it_waits_on_and_what_holds_it(why_fixture):
    answer = _why(WAITING_PLAN, WAITING_SECTION)

    assert answer["view"] == "why"
    assert answer["ref"] == f"{WAITING_PLAN}#{WAITING_SECTION}"
    assert answer["what"]["kind"] == "section"
    assert answer["what"]["state"] == "implementable"

    by_ref = {row["ref"]: row for row in answer["waits_on"]}

    # The declared wait, with its own kind from the section-edge vocabulary.
    held = by_ref[f"{HELD_PLAN}#{HELD_SECTION}"]
    assert held["kind"] == SECTION_DEPENDENCY_BLOCKER
    assert held["satisfied"] is False
    assert held["depth"] == 1
    # The run working on it and the path it holds.
    assert held["runs"] == [RUN_ID]
    assert held["held_paths"] == [HELD_PATH]
    assert held["hold"] == HELD_PATH_KIND

    # The transitive half: B itself waits on gamma#s1, so the read names it too.
    downstream = by_ref[f"{DOWNSTREAM_PLAN}#{DOWNSTREAM_SECTION}"]
    assert downstream["depth"] == 2
    assert downstream["kind"] == SECTION_DEPENDENCY_BLOCKER


def test_the_read_names_the_open_followup_on_the_ref(why_fixture):
    answer = _why(WAITING_PLAN, WAITING_SECTION)

    (followup,) = answer["followups"]
    assert followup["id"] == "next"
    assert followup["status"] == "open"
    assert followup["written_at"] == WRITTEN_AT


def test_the_read_carries_the_runs_and_sessions_working_on_the_ref(why_fixture):
    answer = _why(HELD_PLAN, HELD_SECTION)

    (row,) = answer["runs"]
    assert row["run_id"] == RUN_ID
    assert row["session"] == SESSION
    assert row["held_paths"] == [HELD_PATH]
    assert row["classification"] == "running"


def test_the_read_names_what_waits_on_the_ref(why_fixture):
    answer = _why(HELD_PLAN, HELD_SECTION)

    (waiting,) = answer["waited_on_by"]
    assert waiting["ref"] == f"{WAITING_PLAN}#{WAITING_SECTION}"
    assert waiting["kind"] == SECTION_DEPENDENCY_BLOCKER


def test_the_read_carries_at_most_five_events(why_fixture):
    answer = _why(WAITING_PLAN, WAITING_SECTION)

    # Five, read from the requirement rather than from the constant the answer
    # trims to, so a constant moved with the shared table is still measured.
    assert 0 < len(answer["events"]) <= 5
    assert answer["events"][0]["event"] == "followup-written"


def test_the_answer_fits_its_size_contract(why_fixture):
    answer = _why(WAITING_PLAN, WAITING_SECTION)

    # The measure that would fail if the composition grew unbounded: the answer
    # is served to a caller inside the row the view is built to, not refused for
    # being over a ceiling.
    # The row the shared budget table declares for this view, not a constant
    # this module owns: the contract is the table's, so the measure must read it.
    assert serialised_characters(answer) <= VIEW_BUDGETS[("read_plan", "why")]
    assert answer.get("error") != "response_over_budget"


def test_a_section_the_plan_does_not_declare_is_refused_with_the_nearest(why_fixture):
    answer = _why(WAITING_PLAN, "s9")

    assert answer["error"] == "section_not_found"
    assert f"{WAITING_PLAN}#{WAITING_SECTION}" in answer["hint"]


def test_a_plan_that_does_not_resolve_is_refused_with_the_nearest(why_fixture):
    answer = _why("alpa", None)

    assert answer["error"] == "ref_not_found"
    assert WAITING_PLAN in answer["hint"]


def test_a_plan_level_ref_names_the_runs_on_any_of_its_sections(why_fixture):
    """A held section makes its plan held, so the plan ref names that run."""

    answer = _why(HELD_PLAN, None)

    assert answer["ref"] == HELD_PLAN
    (row,) = answer["runs"]
    assert row["run_id"] == RUN_ID
    assert row["section"] == HELD_SECTION
    assert row["held_paths"] == [HELD_PATH]


def test_a_wait_carries_the_kinds_that_hold_it(why_fixture):
    """The holds are the roadmap's: a decision and a gate, each with its kind."""

    _write_plan(
        why_fixture,
        "delta",
        sections={WAITING_SECTION: "implementable"},
        decisions={"pick-a-route": [WAITING_SECTION]},
    )
    _write_plan(
        why_fixture,
        "epsilon",
        # A gate names one section while other work stays reachable, which is
        # when the roadmap reports it as a section hold.
        sections={WAITING_SECTION: "implementable", "s2": "implementable"},
        gates=["gate-one"],
    )
    reload_mcp_family()

    decided = _why("delta", WAITING_SECTION)
    (decision,) = decided["holds"]
    assert decision["kind"] == DECISION_BLOCKER
    assert decision["id"] == "pick-a-route"

    gated = _why("epsilon", WAITING_SECTION)
    (gate,) = gated["holds"]
    assert gate["kind"] == GATE_BLOCKER
    assert gate["id"] == "gate-one"
