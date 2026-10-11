"""A section-level relation, or an inbound gate, wires a plan.

``unwired-plan`` fired on a plan whose only relation to the rest of the
portfolio was a section-level one — a ``plan-section-depends-on`` wait or a
soft ``plan-after`` — because the rule read only the plan-level links, gates
and the standalone reason. It fired too on a plan another plan's gate names as
its required evidence, which the gate is waiting on. Both are wires the roadmap
follows, so neither leaves a plan unwired.

The audit runs over a synthetic project: plan ``x`` declares only
``plan-section-depends-on`` on ``y#s2`` and holds a gate whose required
evidence names ``y``; plan ``y`` declares nothing and is named only by that
gate; plan ``z`` declares nothing. ``z`` is reported unwired, ``x`` and ``y``
are not.
"""

from __future__ import annotations

import json
from pathlib import Path

from reckon.doccheck import (
    audit_file,
    gate_evidence_slugs,
    run,
    unwired_plan_finding,
)
from reckon.roadmap import build_roadmap

PROJECT = "section-relations-project"
ENFORCED_FROM = "2026-09-19"


def _attr_json(value: object) -> str:
    """A JSON value escaped for a double-quoted HTML attribute."""

    return json.dumps(value).replace('"', "&quot;")


def _plan_page(
    slug: str,
    *,
    section_depends_on: dict[str, list[str]] | None = None,
    after: str | None = None,
    gate_evidence: str | None = None,
    status: str = "active",
    modified: str = ENFORCED_FROM,
) -> str:
    metas = [
        ("docs-project", PROJECT),
        ("reckon-type", "plan"),
        ("plan-slug", slug),
        ("plan-status", status),
        ("plan-modified", modified),
    ]
    if after is not None:
        metas.append(("plan-after", after))
    if section_depends_on is not None:
        metas.append(("plan-section-depends-on", _attr_json(section_depends_on)))
    head = "".join(f'<meta name="{name}" content="{value}">' for name, value in metas)
    gates = ""
    if gate_evidence is not None:
        gates = (
            '<section data-reckon="gates" id="gates" class="r-gates">'
            "<h2>Evidence gates</h2>"
            '<div class="r-gate" data-id="g-x" data-status="open">'
            '<p class="r-gate-measure">x waits on y supplying its anchor</p>'
            f'<p class="r-gate-required-evidence">{gate_evidence}</p>'
            "</div></section>"
        )
    return (
        '<!doctype html><html lang="en"><head>'
        f"{head}<title>{slug}</title></head>"
        f'<body><main class="plan-doc">{gates}</main></body></html>'
    )


def _project(tmp_path: Path) -> dict[str, Path]:
    """The synthetic project: ``x``, ``y``, ``z`` plus two section-wire only plans."""

    docs = tmp_path / "docs" / "plans"
    docs.mkdir(parents=True)
    pages = {
        "x": _plan_page(
            "x",
            section_depends_on={"s1": ["y#s2"]},
            gate_evidence="y#s2",
        ),
        "y": _plan_page("y"),
        "z": _plan_page("z"),
        "section-only": _plan_page("section-only", section_depends_on={"s1": ["y#s2"]}),
        "after-only": _plan_page("after-only", after="x"),
    }
    paths = {}
    for slug, text in pages.items():
        path = docs / f"{slug}.html"
        path.write_text(text, encoding="utf-8")
        paths[slug] = path
    return paths


def _unwired(path: Path, paths: dict[str, Path]) -> list[str]:
    inbound = gate_evidence_slugs(paths.values(), project=PROJECT)
    return [
        finding.code
        for finding in audit_file(path, project=PROJECT, inbound_evidence=inbound)
        if finding.code == "unwired-plan"
    ]


# ── the finding's inputs ────────────────────────────────────────────────────


def test_a_section_wire_silences_the_finding():
    assert (
        unwired_plan_finding(
            doc_type="plan",
            status="active",
            modified=ENFORCED_FROM,
            links=[],
            gate_count=0,
            standalone=None,
            slug="x",
            section_wires=True,
        )
        is None
    )


def test_inbound_gate_evidence_silences_the_finding():
    assert (
        unwired_plan_finding(
            doc_type="plan",
            status="active",
            modified=ENFORCED_FROM,
            links=[],
            gate_count=0,
            standalone=None,
            slug="y",
            inbound_evidence=True,
        )
        is None
    )


def test_a_plan_with_no_wire_is_still_reported():
    finding = unwired_plan_finding(
        doc_type="plan",
        status="active",
        modified=ENFORCED_FROM,
        links=[],
        gate_count=0,
        standalone=None,
        slug="z",
    )

    assert finding is not None
    assert finding.severity == "error"


# ── the audit over the project ──────────────────────────────────────────────


def test_audit_reports_only_the_unwired_plan(tmp_path: Path):
    paths = _project(tmp_path)

    inbound = gate_evidence_slugs(paths.values(), project=PROJECT)

    assert inbound == {"y"}

    reported = {slug: _unwired(path, paths) for slug, path in paths.items()}

    assert reported["z"] == ["unwired-plan"]
    for slug in ("x", "y", "section-only", "after-only"):
        assert reported[slug] == [], slug


def test_run_threads_inbound_evidence_across_the_corpus(tmp_path, capsys):
    paths = _project(tmp_path)

    exit_code = run([str(p) for p in paths.values()], project=PROJECT)
    out = capsys.readouterr().out

    status: dict[str, str] = {}
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in out.splitlines():
        head, sep, rest = line.partition(": ")
        if sep and head and Path(head).exists():
            current = Path(head).name
            status[current] = rest
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)

    wiring = {
        name
        for name, lines in blocks.items()
        if any("unwired-plan" in ln for ln in lines)
    }

    assert status["z.html"].startswith("FAIL")
    for name in ("x.html", "y.html", "section-only.html", "after-only.html"):
        assert status[name] == "OK", name
    assert wiring == {"z.html"}
    # z carries the only error, so the run fails.
    assert exit_code == 1


# ── the roadmap and the write boundary see the same wires ────────────────────


def _row(**overrides) -> dict:
    row = {
        "slug": "p",
        "type": "plan",
        "status": "active",
        "modified": ENFORCED_FROM,
        "depends_on": [],
        "blocks": [],
        "informs": [],
        "gates": [],
    }
    row.update(overrides)
    return row


def test_roadmap_silences_section_and_inbound_wires():
    report = build_roadmap(
        PROJECT,
        [
            _row(
                slug="x",
                gates=[
                    {"id": "g-x", "required_evidence": "y#s2", "verdict": "pending"}
                ],
                section_depends_on={"s1": ["y#s2"]},
            ),
            _row(slug="y"),
            _row(slug="z"),
            _row(slug="section-only", section_depends_on={"s1": ["y#s2"]}),
            _row(slug="after-only", after=["x"]),
        ],
        [],
    )

    reported = sorted(
        finding["slug"]
        for finding in report["wiring_findings"]
        if finding["code"] == "unwired-plan"
    )

    assert reported == ["z"]


def test_the_new_plan_refusal_reads_a_declared_section_wire():
    from reckon.mcp_discovery import _unwired_plan_refusal

    working = {"status": "active", "modified": ENFORCED_FROM, "after": ["x"]}

    assert _unwired_plan_refusal("soft", working) is None


def test_the_new_plan_refusal_still_refuses_a_plan_with_no_wire():
    from reckon.mcp_discovery import _unwired_plan_refusal

    working = {"status": "active", "modified": ENFORCED_FROM}

    assert _unwired_plan_refusal("bare", working) is not None
