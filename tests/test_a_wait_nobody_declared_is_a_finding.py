"""The wiring findings name a wait nobody declared and a scope two plans share.

Two coordinators carrying the same edge in messages is the failure these
findings exist to make visible: a section held by something that names another
section it never declared a dependency on, and two plans' open sections fencing
the same files. Each is reported through the shared finding helper and neither
invents the edge — the analyser reports the suspected wait and leaves the
decision to whoever owns the plan.

Every fixture drives the real classifier and the real analyser. The blocked run
is a pointer whose manifest reports blocked, so the mentioning text is the
manifest's own blocker line rather than a string handed to the analyser.
"""

from __future__ import annotations

from reckon import roadmap

PROJECT = "sample"


def _plan(
    slug: str,
    *,
    sections: dict[str, str] | None = None,
    section_depends_on: dict[str, list[str]] | None = None,
    gates: list[dict] | None = None,
    followups: list[dict] | None = None,
) -> dict:
    row = {
        "slug": slug,
        "title": slug,
        "type": "plan",
        "status": "active",
        "impl": 0.3,
        "depends_on": [],
        "effort": "M",
        "roi": "high",
        "blocking": [],
        "gates": gates or [],
        "followups": followups or [],
        "section_declarations": sections or {"s1": "implementable"},
        "sections": [],
    }
    if section_depends_on is not None:
        row["section_depends_on"] = section_depends_on
    return row


def _sprint(*slugs: str) -> dict:
    return {"id": "S1", "status": "active", "items": list(slugs)}


def _blocked_pointer(
    tmp_path,
    *,
    plan: str,
    section: str,
    run_id: str,
    blocker_text: str,
) -> dict:
    """A live pointer whose manifest reports blocked, naming ``blocker_text``."""

    manifest = tmp_path / f"{run_id}.md"
    manifest.write_text(f"node: {run_id}\nstatus: blocked\nblockers: {blocker_text}\n")
    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-one",
        "created_at": "2026-08-12T18:00:00Z",
        "phase": "working",
        "process_alive": False,
        "launched_at": "2026-08-12T18:00:00Z",
        "node": {"id": run_id, "plan": plan, "section": section},
        "manifest_path": str(manifest),
    }


def _running_pointer(*, plan: str, section: str, run_id: str) -> dict:
    return {
        "run_id": run_id,
        "project": PROJECT,
        "member": "worker-two",
        "created_at": "2026-08-12T18:00:00Z",
        "phase": "working",
        "process_alive": True,
        "launcher_host": "another-login-node",
        "node": {"id": run_id, "plan": plan, "section": section},
    }


def _wiring(report: dict, code: str) -> list[dict]:
    return [
        finding for finding in report["wiring_findings"] if finding.get("code") == code
    ]


def _write_claims(monkeypatch, claims: list[dict]) -> None:
    monkeypatch.setattr(
        "reckon.roadmap._read_scope_claims", lambda *args, **kwargs: claims
    )


def test_the_extraction_returns_the_refs_a_literal_text_names():
    """What the reader produces, on literals the test writes down itself.

    The fixture below hands the analyser a blocker string and then reads that
    same string back off the finding, which cannot show the extraction working:
    an empty extraction echoes the string just as faithfully. These cases fix
    the answer to inputs this test chose, so the reader is measured rather than
    the round trip.
    """

    assert roadmap._mentioned_section_refs("wait beta#s4 must land first") == ["beta#s4"]
    assert roadmap._mentioned_section_refs("wait beta#s4, then gamma#s2.") == [
        "beta#s4",
        "gamma#s2",
    ]
    assert roadmap._mentioned_section_refs("also other:delta#s3 does") == ["other:delta#s3"]
    assert roadmap._mentioned_section_refs("beta#s4 beta#s4") == ["beta#s4"]
    assert roadmap._mentioned_section_refs("no reference here") == []


def test_a_blocked_run_naming_an_undeclared_section_is_reported(monkeypatch, tmp_path):
    """The section is held by a wait it never declared, and the text says so."""

    pointer = _blocked_pointer(
        tmp_path,
        plan="alpha",
        section="s1",
        run_id="run-blocked",
        blocker_text="waiting on the integrated sweep; beta#s4 must land first",
    )
    monkeypatch.setattr("reckon.crew.list_live", lambda: [pointer])
    _write_claims(monkeypatch, [])

    report = roadmap.build_roadmap(
        PROJECT,
        [_plan("alpha"), _plan("beta", sections={"s4": "implementable"})],
        [_sprint("alpha", "beta")],
    )

    findings = _wiring(report, "undeclared-wait")
    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "warn"
    assert finding["extra"]["waiting"] == "alpha#s1"
    assert finding["extra"]["ref"] == "beta#s4"
    assert finding["extra"]["source"] == "blocked run"
    assert "beta#s4" in finding["message"]
    assert "alpha#s1" in finding["message"]


def test_the_same_fixture_with_the_edge_declared_emits_nothing(monkeypatch, tmp_path):
    """A declared edge is the wait the finding would have asked for."""

    pointer = _blocked_pointer(
        tmp_path,
        plan="alpha",
        section="s1",
        run_id="run-blocked",
        blocker_text="waiting on the integrated sweep; beta#s4 must land first",
    )
    monkeypatch.setattr("reckon.crew.list_live", lambda: [pointer])
    _write_claims(monkeypatch, [])

    report = roadmap.build_roadmap(
        PROJECT,
        [
            _plan("alpha", section_depends_on={"s1": ["beta#s4"]}),
            _plan("beta", sections={"s4": "implementable"}),
        ],
        [_sprint("alpha", "beta")],
    )

    assert _wiring(report, "undeclared-wait") == []


def test_a_gate_measure_naming_an_undeclared_section_is_reported(monkeypatch, tmp_path):
    """A gate is the second source: its measure names the section it waits on."""

    monkeypatch.setattr("reckon.crew.list_live", list)
    _write_claims(monkeypatch, [])

    report = roadmap.build_roadmap(
        PROJECT,
        [
            _plan(
                "alpha",
                sections={"s1": "implementable", "s2": "implementable"},
                gates=[
                    {
                        "id": "g1",
                        "measure": "the sweep cannot close before beta#s4",
                        "gated_sections": ["s1"],
                        "verdict": "",
                        "status": "open",
                    }
                ],
            ),
            _plan("beta", sections={"s4": "implementable"}),
        ],
        [_sprint("alpha", "beta")],
    )

    findings = _wiring(report, "undeclared-wait")
    assert [finding["extra"]["ref"] for finding in findings] == ["beta#s4"]
    assert findings[0]["extra"]["source"] == "gate measure"


def test_two_plans_open_sections_claiming_one_path_are_reported(monkeypatch):
    """Two plans fencing one file cannot see each other's work."""

    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [
            _running_pointer(plan="alpha", section="s1", run_id="run-alpha"),
            _running_pointer(plan="beta", section="s4", run_id="run-beta"),
        ],
    )
    _write_claims(
        monkeypatch,
        [
            {"run_id": "run-alpha", "path": "src/shared_circuit.py", "binding": True},
            {"run_id": "run-beta", "path": "src/shared_circuit.py", "binding": True},
        ],
    )

    report = roadmap.build_roadmap(
        PROJECT,
        [_plan("alpha"), _plan("beta", sections={"s4": "implementable"})],
        [_sprint("alpha", "beta")],
    )

    findings = _wiring(report, "overlapping-scope")
    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "warn"
    assert finding["extra"]["sections"] == ["alpha#s1", "beta#s4"]
    assert finding["extra"]["paths"] == [
        {"left_path": "src/shared_circuit.py", "right_path": "src/shared_circuit.py"}
    ]
    assert "alpha#s1" in finding["message"]
    assert "beta#s4" in finding["message"]


def test_two_plans_open_sections_claiming_disjoint_paths_emit_nothing(monkeypatch):
    """The control: the same two sections, paths that do not touch."""

    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [
            _running_pointer(plan="alpha", section="s1", run_id="run-alpha"),
            _running_pointer(plan="beta", section="s4", run_id="run-beta"),
        ],
    )
    _write_claims(
        monkeypatch,
        [
            {"run_id": "run-alpha", "path": "src/alpha_only.py", "binding": True},
            {"run_id": "run-beta", "path": "src/beta_only.py", "binding": True},
        ],
    )

    report = roadmap.build_roadmap(
        PROJECT,
        [_plan("alpha"), _plan("beta", sections={"s4": "implementable"})],
        [_sprint("alpha", "beta")],
    )

    assert _wiring(report, "overlapping-scope") == []


def test_a_section_declared_done_is_not_reviewed_for_overlapping_scope(monkeypatch):
    """A landed section is closed work, however long its run pointer lingers.

    Openness is the declaration's: a section the plan has ticked is done even
    while the run that landed it still holds a claim, and closing it is the
    plan's own act rather than the dispatcher's. The second half is the control
    — the same two runs and the same one path, with the section still declared
    implementable, do collide.
    """

    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [
            _running_pointer(plan="alpha", section="s1", run_id="run-alpha"),
            _running_pointer(plan="beta", section="s4", run_id="run-beta"),
        ],
    )
    _write_claims(
        monkeypatch,
        [
            {"run_id": "run-alpha", "path": "src/shared_circuit.py", "binding": True},
            {"run_id": "run-beta", "path": "src/shared_circuit.py", "binding": True},
        ],
    )

    landed = roadmap.build_roadmap(
        PROJECT,
        [
            _plan("alpha", sections={"s1": "done"}),
            _plan("beta", sections={"s4": "implementable"}),
        ],
        [_sprint("alpha", "beta")],
    )
    assert _wiring(landed, "overlapping-scope") == []

    still_open = roadmap.build_roadmap(
        PROJECT,
        [_plan("alpha"), _plan("beta", sections={"s4": "implementable"})],
        [_sprint("alpha", "beta")],
    )
    assert len(_wiring(still_open, "overlapping-scope")) == 1


def test_two_sections_of_one_plan_claiming_one_path_emit_nothing(monkeypatch):
    """One plan's sections fencing a file is that plan's own sequencing."""

    monkeypatch.setattr(
        "reckon.crew.list_live",
        lambda: [
            _running_pointer(plan="alpha", section="s1", run_id="run-one"),
            _running_pointer(plan="alpha", section="s2", run_id="run-two"),
        ],
    )
    _write_claims(
        monkeypatch,
        [
            {"run_id": "run-one", "path": "src/shared_circuit.py", "binding": True},
            {"run_id": "run-two", "path": "src/shared_circuit.py", "binding": True},
        ],
    )

    report = roadmap.build_roadmap(
        PROJECT,
        [_plan("alpha", sections={"s1": "implementable", "s2": "implementable"})],
        [_sprint("alpha")],
    )

    assert _wiring(report, "overlapping-scope") == []
