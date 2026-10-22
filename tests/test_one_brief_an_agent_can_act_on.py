"""The brief names the work a coordinator starts from, read off the payload.

The roadmap payload is correct and large. The brief is the few lines of it that
decide what to do next, and it must be a composition rather than a second
analyser: every figure it prints is a field of the payload, so a brief and the
roadmap cannot disagree.

The fixture here is a synthetic three-plan chain whose wiring is
``plan-section-depends-on`` alone, so the critical path runs through sections.
One run, blocked and old, sits on the head section and names a section its
dependencies do not include. The head, the top-ranked ready section, the oldest
blocked run and the undeclared wait are all fixed by the fixture rather than
read back off the brief, and the second arm changes one edge and requires the
brief's critical-path line to move with it.

Every fixture is written into a synthetic docs tree under the test's own
``tmp_path``, and the live-pointer read is patched, so no test reads a mounted
project's state or the running fleet.
"""

from __future__ import annotations

import copy
import json
import os
import time
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any

import pytest

from reckon.project_brief import BRIEF_CHAR_BUDGET, project_brief

PROJECT = "sample"
UPSTREAM = "plan-a"
WAITS_ON_A = "plan-b"
WAITS_ON_B = "plan-c"
INDEPENDENT = "plan-d"
RELEASING = "plan-e"

UPSTREAM_HOURS = 3.0
WAITS_ON_A_HOURS = 5.0
WAITS_ON_B_HOURS = 2.0
INDEPENDENT_HOURS = 1.0

# The three ready sections releasing the most work, in the order the payload's
# reach field forces: the chain's head releases the middle plan's section, the
# releasing plan's first section releases its own second, and the independent
# plan releases nothing. Written out here so the brief's reach list is
# compared against the fixture rather than against a repeat of its own sort.
EXPECTED_REACH = [
    (f"{UPSTREAM}#s2", 1),
    (f"{RELEASING}#s1", 1),
    (f"{INDEPENDENT}#s1", 0),
]

# The chain the fixture's edges force, deepest prerequisite first. Written out
# here so a brief naming something else is compared against the fixture rather
# than against itself.
EXPECTED_HEAD = f"{UPSTREAM}#s2"
EXPECTED_CHAIN = [f"{UPSTREAM}#s2", f"{WAITS_ON_A}#s3", f"{WAITS_ON_B}#s1"]
BLOCKED_RUN = "run-blocked-old"
BLOCKER_TEXT = f"stopped until {WAITS_ON_B}#s1 lands"
UNDECLARED_REF = f"{WAITS_ON_B}#s1"


@pytest.fixture()
def mounted_project(tmp_path, monkeypatch):
    docs = tmp_path / "repo" / "docs"
    docs.mkdir(parents=True)
    state_root = tmp_path / "state"
    state_root.mkdir()
    mounts = tmp_path / "mounts.json"
    mounts.write_text(json.dumps({PROJECT: str(docs)}))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts))
    monkeypatch.setenv("RECKON_STATE_ROOT", str(state_root))

    import reckon.serve as serve_module

    serve_module._MOUNTS_FILE = mounts
    serve_module._STATE_ROOT = state_root
    serve_module._DISC_CACHE.clear()
    from tests.mcp_family_reload import reload_mcp_family

    reload_mcp_family()
    return docs


def _write_plan(
    docs: Path,
    slug: str,
    *,
    sections: dict[str, float],
    section_depends_on: dict[str, list[str]] | None = None,
) -> Path:
    """Write one plan holding its open sections, each at its own effort."""

    def meta(name: str, value: Any) -> str:
        return f'<meta name="{name}" content="{escape(value, quote=True)}">'

    declarations = json.dumps(
        dict.fromkeys(sections, "implementable"), separators=(",", ":")
    )
    head = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f'<meta name="docs-project" content="{PROJECT}">',
        '<meta name="reckon-type" content="plan">',
        meta("plan-slug", slug),
        f'<meta name="plan-title" content="{slug}">',
        '<meta name="plan-status" content="active">',
        meta("plan-section-declarations", declarations),
    ]
    if section_depends_on:
        head.append(
            meta(
                "plan-section-depends-on",
                json.dumps(section_depends_on, separators=(",", ":")),
            )
        )
    head.append(f"<title>{slug}</title></head>")
    body = ['<body><main class="plan-doc">']
    for name, hours in sorted(sections.items()):
        body.append(
            f'<h2 id="{name}">Section</h2>'
            '<section data-reckon="section"'
            f' data-id="{name}"'
            f' data-effort-hours="{hours}"'
            ' data-capability-version="1.0" data-capability-class="general"'
            ' data-capability-reasoning="standard"'
            ' data-capability-verification="standard"'
            ' data-capability-risk="low"'
            ' data-status="implementable" data-links=""></section>'
        )
    body.append("</main></body></html>")
    path = docs / "plans" / f"{slug}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(head) + "".join(body), encoding="utf-8")
    return path


def _write_chain(docs: Path) -> None:
    """The three-plan chain whose wiring is ``plan-section-depends-on`` alone."""

    _write_plan(docs, UPSTREAM, sections={"s2": UPSTREAM_HOURS})
    _write_plan(
        docs,
        WAITS_ON_A,
        sections={"s3": WAITS_ON_A_HOURS},
        section_depends_on={"s3": [f"{UPSTREAM}#s2"]},
    )
    _write_plan(
        docs,
        WAITS_ON_B,
        sections={"s1": WAITS_ON_B_HOURS},
        section_depends_on={"s1": [f"{WAITS_ON_A}#s3"]},
    )


@pytest.fixture()
def three_plan_project(mounted_project):
    _write_chain(mounted_project)
    _write_plan(mounted_project, INDEPENDENT, sections={"s1": INDEPENDENT_HOURS})
    _write_plan(
        mounted_project,
        RELEASING,
        sections={"s1": INDEPENDENT_HOURS, "s2": INDEPENDENT_HOURS},
        section_depends_on={"s2": [f"{RELEASING}#s1"]},
    )
    return mounted_project


def _pointer(
    *,
    run_id: str,
    plan: str,
    section: str,
    manifest: Path | None = None,
    log: Path | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-one",
        "created_at": "2026-10-01T00:00:00Z",
        "phase": "working",
        "process_alive": False,
        "launcher_host": "another-login-node",
        "node": {"id": f"{plan}-{section}", "plan": plan, "section": section},
    }
    if manifest is not None:
        record["manifest_path"] = str(manifest)
    if log is not None:
        record["log_path"] = str(log)
    return record


def _blocked_pointer(tmp_path: Path) -> dict[str, Any]:
    """One run on the head section, blocked, and naming an undeclared wait."""

    manifest = tmp_path / "manifest.md"
    log = tmp_path / "log.jsonl"
    now = time.time()
    manifest.write_text(
        f"node: {UPSTREAM}-s2\n"
        "status: blocked\n"
        "commits: HEAD\n"
        "blockers:\n"
        f'  - "{BLOCKER_TEXT}"\n'
    )
    log.write_text('{"type":"turn.started"}\n')
    os.utime(manifest, (now - 10, now - 10))
    os.utime(log, (now, now))
    return _pointer(
        run_id=BLOCKED_RUN,
        plan=UPSTREAM,
        section="s2",
        manifest=manifest,
        log=log,
    )


def _roadmap(docs: Path, pointers: list[dict[str, Any]], monkeypatch) -> dict[str, Any]:
    """Build the payload the way the CLI does, from the fixture's docs tree.

    Discovery reads the plans and sprints off the tree, so the payload is the
    one the command line would compose, and the live fleet is reduced to the
    fixture's pointers so no test reads the running fleet.
    """

    monkeypatch.setattr("reckon.crew.list_live", lambda *a, **k: list(pointers))
    from reckon.mcp import _roadmap as mcp_roadmap

    # ``checkout_path`` is the repository root, whose ``docs`` the scan reads.
    return mcp_roadmap(PROJECT, str(docs.parent))


def test_the_brief_names_the_head_the_top_section_the_blocked_run_and_the_wait(
    three_plan_project, monkeypatch, tmp_path
):
    """All four the fixture fixes appear in the brief, within its budget.

    The head and the oldest blocked run are the ones the fixture wired; the
    top-ranked ready section and the undeclared wait are read off the payload so
    the brief is compared against the fixture, not against itself.
    """

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    brief = project_brief(
        PROJECT, payload=payload, now=datetime(2026, 10, 22, tzinfo=UTC)
    )

    assert payload["critical_path"]["sections"] == EXPECTED_CHAIN
    assert len(brief.text) <= BRIEF_CHAR_BUDGET

    # The head is the fixture's deepest prerequisite.
    assert brief.row("critical_path")["head"] == EXPECTED_HEAD
    assert EXPECTED_HEAD in brief.text

    # The head carries the blocked run.
    assert BLOCKED_RUN in brief.row("critical_path")["head_runs"]

    # The top-ranked ready section is the payload's first ready section row.
    first_ready = next(
        row["ref"]
        for row in payload["immediate_roadmap"]
        if row.get("ref") and row.get("ready")
    )
    assert brief.row("ready_sections")["sections"][0]["ref"] == first_ready
    assert first_ready in brief.text

    # The oldest blocked run is named.
    assert brief.row("at_risk")["oldest_blocked_run"] == BLOCKED_RUN
    assert BLOCKED_RUN in brief.text

    # The undeclared wait is a wiring finding, and the brief names it.
    codes = brief.row("wiring")["counts"]
    assert codes.get("undeclared-wait", 0) >= 1
    assert "undeclared-wait" in brief.text
    assert UNDECLARED_REF in brief.text


def test_the_oldest_blocked_run_is_the_oldest_one_the_brief_can_see(
    three_plan_project, monkeypatch, tmp_path
):
    """With two blocked runs, the brief names the older one.

    The other reading — the newest, or the first in the payload's order — is
    what a brief that picked a blocked run without comparing their ages would
    print."""

    older = _blocked_pointer(tmp_path)
    newer_manifest = tmp_path / "manifest-newer.md"
    newer_log = tmp_path / "log-newer.jsonl"
    now = time.time()
    newer_manifest.write_text(
        f'node: {INDEPENDENT}-s1\nstatus: blocked\nblockers:\n  - "late stop"\n'
    )
    newer_log.write_text('{"type":"turn.started"}\n')
    os.utime(newer_manifest, (now - 10, now - 10))
    os.utime(newer_log, (now, now))
    newer = _pointer(
        run_id="run-blocked-new",
        plan=INDEPENDENT,
        section="s1",
        manifest=newer_manifest,
        log=newer_log,
    )
    newer["created_at"] = "2026-10-20T00:00:00Z"

    payload = _roadmap(three_plan_project, [older, newer], monkeypatch)
    brief = project_brief(
        PROJECT, payload=payload, now=datetime(2026, 10, 22, tzinfo=UTC)
    )

    assert set(brief.row("at_risk")["blocked_runs"]) == {BLOCKED_RUN, "run-blocked-new"}
    assert brief.row("at_risk")["oldest_blocked_run"] == BLOCKED_RUN


def test_one_changed_fixture_edge_moves_the_critical_path_line(
    mounted_project, monkeypatch, tmp_path
):
    """Changing one declared edge changes the brief's critical-path line.

    The line is the chain the payload carries, not a restatement of the plans
    involved, so a rewire that drops a plan out of the path moves it."""

    _write_chain(mounted_project)
    wired = _roadmap(mounted_project, [], monkeypatch)
    wired_brief = project_brief(PROJECT, payload=wired)

    # The sibling tree drops B from the chain: ``plan-c#s1`` waits on
    # ``plan-a#s2`` directly.
    docs_alt = mounted_project.parent.parent / "repo-alt" / "docs"
    docs_alt.mkdir(parents=True)
    _write_plan(docs_alt, UPSTREAM, sections={"s2": UPSTREAM_HOURS})
    _write_plan(
        docs_alt,
        WAITS_ON_B,
        sections={"s1": WAITS_ON_B_HOURS},
        section_depends_on={"s1": [f"{UPSTREAM}#s2"]},
    )
    rewired = _roadmap(docs_alt, [], monkeypatch)
    rewired_brief = project_brief(PROJECT, payload=rewired)

    assert wired_brief.row("critical_path")["sections"] == EXPECTED_CHAIN
    assert rewired_brief.row("critical_path")["sections"] == [
        f"{UPSTREAM}#s2",
        f"{WAITS_ON_B}#s1",
    ]
    assert (
        wired_brief.line("critical_path").text
        != rewired_brief.line("critical_path").text
    )


def test_every_figure_in_the_brief_equals_its_roadmap_field(
    three_plan_project, monkeypatch, tmp_path
):
    """The brief is a composition: each figure is the payload's own field.

    A brief that recomputed a figure would drift from the roadmap silently; this
    compares each back against the field it came from."""

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    brief = project_brief(
        PROJECT, payload=payload, now=datetime(2026, 10, 22, tzinfo=UTC)
    )

    overview = brief.row("overview")
    assert (
        overview["lifecycle_completion_pct"]
        == payload["completion"]["lifecycle_completion_pct"]
    )
    assert overview["implementation_pct"] == payload["completion"]["implementation_pct"]
    assert overview["plans"] == payload["completion"]["plans"]
    assert overview["ready"] == len(payload["ready_now"])
    assert overview["blocked"] == len(payload["blocked"])
    assert overview["deferred"] == len(payload["deferred"])

    critical = brief.row("critical_path")
    assert critical["sections"] == payload["critical_path"]["sections"]
    assert critical["plans"] == payload["critical_path"]["plans"]
    assert critical["length_hours"] == payload["critical_path"]["length_hours"]
    assert critical["worker_hours"] == payload["critical_path"]["worker_hours"]
    assert critical["head"] == payload["critical_path"]["sections"][0]

    ranked = [
        row
        for row in payload["immediate_roadmap"]
        if row.get("ref") and row.get("ready")
    ][:10]
    assert brief.row("ready_sections")["sections"] == [
        {
            "ref": row["ref"],
            "plan": row["plan"],
            "reason": row["reason"],
            "unlocks": len(row.get("unlocks") or []),
        }
        for row in ranked
    ]

    # Three is the fixture's own fact, and it is compared against the fixture's
    # own expectation rather than against a repeat of the brief's own sort.
    assert len(ranked) >= 3
    assert [
        (entry["ref"], entry["unlocks"])
        for entry in brief.row("ready_sections")["largest_reach"]
    ] == EXPECTED_REACH

    assert brief.row("wiring")["total"] == len(payload["wiring_findings"])
    assert brief.row("wiring")["top"] == [
        {
            "code": str(finding.get("code") or ""),
            "severity": str(finding.get("severity") or ""),
            "message": str(finding.get("message") or ""),
        }
        for finding in payload["wiring_findings"][:3]
    ]


def test_the_brief_reports_what_moved_since_a_given_time(
    three_plan_project, monkeypatch, tmp_path
):
    """With ``since``, the brief adds the work that started after it."""

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    before = project_brief(PROJECT, since="2026-09-01T00:00:00Z", payload=payload)

    assert before.line("since") is not None
    assert BLOCKED_RUN in before.row("since")["runs_started"]

    after = project_brief(PROJECT, since="2026-11-01T00:00:00Z", payload=payload)
    assert after.row("since")["runs_started"] == []


def test_the_brief_guard_refuses_a_composition_over_its_budget(
    three_plan_project, monkeypatch, tmp_path
):
    """The budget is enforced, not merely intended.

    A payload holding more interrupted runs than a page can carry makes the
    composed text exceed the budget; a brief that silently truncated would
    report less than the payload holds, so the guard refuses."""

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    inflated = copy.deepcopy(payload)
    inflated["sprints"] = [
        {
            "id": "fixture-sprint",
            "interrupted": [
                {
                    "run_id": f"run-interrupted-{index:04d}-" + "z" * 24,
                    "classification": "interrupted",
                    "started_at": "2026-10-01T00:00:00Z",
                }
                for index in range(400)
            ],
        }
    ]

    with pytest.raises(ValueError, match="over the"):
        project_brief(PROJECT, payload=inflated)


def _invoke_brief(arguments: list[str], monkeypatch, payload: Any):
    """Run the roadmap verb through Click with the scan replaced by a payload.

    The verb reads its report from ``reckon.mcp._roadmap``, so patching that
    name exercises the command's own branches — the print path, the portfolio
    split and the refusals — without a mounted-project scan.
    """

    monkeypatch.setattr("reckon.mcp._roadmap", lambda *args, **kwargs: payload)
    from click.testing import CliRunner

    from reckon import cli as cli_module

    return CliRunner().invoke(cli_module.main, ["roadmap", *arguments])


def test_the_brief_prints_from_the_command_line_for_one_project(
    three_plan_project, monkeypatch, tmp_path
):
    """``--brief`` prints the composed page for the project named."""

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    result = _invoke_brief(["--project", PROJECT, "--brief"], monkeypatch, payload)

    assert result.exit_code == 0, result.output
    assert EXPECTED_HEAD in result.output
    assert BLOCKED_RUN in result.output


def test_the_brief_prints_one_page_per_project_in_a_portfolio(
    three_plan_project, monkeypatch, tmp_path
):
    """A portfolio read prints one brief per project that was read.

    A project whose report failed carries no payload to compose a brief from,
    so it is skipped rather than printed as an empty page beside the rest.
    """

    first = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    second = copy.deepcopy(first)
    second["project"] = "second-project"
    second["critical_path"] = {"sections": ["second#s1"], "plans": ["second"]}
    failed = {"project": "third-project", "ok": False, "detail": "scan refused"}

    result = _invoke_brief(
        ["--project", "*", "--brief"],
        monkeypatch,
        {"projects": [first, second, failed]},
    )

    assert result.exit_code == 0, result.output
    assert first["project"] in result.output
    assert "second-project" in result.output
    assert "second#s1" in result.output
    assert "third-project" not in result.output


def test_the_brief_refuses_over_budget_with_a_message_not_a_traceback(
    three_plan_project, monkeypatch, tmp_path
):
    """A composition the budget refuses reaches the reader as a message.

    The guard raises, and the command line must print the reason rather than
    let the exception out as an unhandled traceback.
    """

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)
    inflated = copy.deepcopy(payload)
    inflated["sprints"] = [
        {
            "id": "fixture-sprint",
            "interrupted": [
                {
                    "run_id": f"run-interrupted-{index:04d}-" + "z" * 24,
                    "classification": "interrupted",
                    "started_at": "2026-10-01T00:00:00Z",
                }
                for index in range(400)
            ],
        }
    ]

    result = _invoke_brief(["--project", PROJECT, "--brief"], monkeypatch, inflated)

    assert result.exit_code == 1, result.output
    assert "Error:" in result.output
    assert "over the" in result.output
    assert "Traceback" not in result.output


def test_a_change_window_without_a_brief_is_refused_before_the_scan(
    three_plan_project, monkeypatch, tmp_path
):
    """``--since`` names a thing only the brief reads, so the pair is required.

    The refusal is a usage error and it is raised before the report is read, so
    a mistyped invocation costs no scan and says which option is missing.
    """

    payload = _roadmap(three_plan_project, [_blocked_pointer(tmp_path)], monkeypatch)

    def refused(*args, **kwargs):
        raise AssertionError("the scan ran before the option pair was checked")

    monkeypatch.setattr("reckon.mcp._roadmap", refused)
    from click.testing import CliRunner

    from reckon import cli as cli_module

    without = CliRunner().invoke(
        cli_module.main,
        ["roadmap", "--project", PROJECT, "--since", "2026-09-01T00:00:00Z"],
    )

    assert without.exit_code == 2, without.output
    assert "--brief" in without.output

    with_brief = _invoke_brief(
        ["--project", PROJECT, "--brief", "--since", "2026-09-01T00:00:00Z"],
        monkeypatch,
        payload,
    )

    assert with_brief.exit_code == 0, with_brief.output
    assert BLOCKED_RUN in with_brief.output
