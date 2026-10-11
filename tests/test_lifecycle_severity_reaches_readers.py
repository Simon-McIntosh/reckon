"""Each lifecycle finding's own severity reaches the MCP and CLI audit readers.

A lifecycle row carries a severity that its flag alone cannot recover: a
shipped plan still listed by an open sprint is ``info``, not the ``warn`` a
flag-derived rule would assign. The MCP audit read and the ``reckon audit``
table each read the finding's own severity, falling back to the flag-derived
value only for a finding that carries none (the foreign-followup rows).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import doccheck
from reckon.mcp import _audit
from reckon.project_maintenance_commands import main

PROJECT = "proj"
MEMBER_SPRINT = "S26"
OTHER_SPRINT = "S27"


def _plan_html(
    slug: str,
    *,
    sprint: str | None,
    status: str = "active",
    impl: str = "1.0",
) -> str:
    metas = (
        f'<meta name="plan-slug" content="{slug}">'
        '<meta name="reckon-type" content="plan">'
        f'<meta name="plan-status" content="{status}">'
        f'<meta name="plan-impl" content="{impl}">'
        '<meta name="plan-modified" content="2026-10-01">'
        f'<meta name="docs-project" content="{PROJECT}">'
    )
    if sprint is not None:
        metas += f'<meta name="plan-sprint" content="{sprint}">'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{metas}<title>{slug}</title></head>"
        '<body><main class="plan-doc"><h2 id="s1">Body</h2></main></body></html>'
    )


def _sprint_html(sprint_id: str, *, status: str, members: list[str]) -> str:
    state = {
        "type": "sprint",
        "id": sprint_id,
        "status": status,
        "theme": sprint_id,
        "items": [{"slug": member} for member in members],
        "version": 1,
    }
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta name="docs-project" content="{PROJECT}">'
        '<meta name="reckon-type" content="sprint">'
        f'<meta name="reckon-id" content="{sprint_id}">'
        '<meta name="reckon-version" content="1">'
        f"<title>{sprint_id}</title></head><body>"
        f'<main class="reckon-resource" data-type="sprint" data-id="{sprint_id}">'
        '<ol data-reckon="sprint-items"></ol>'
        '<script type="application/json" id="reckon-resource-state">'
        f"{json.dumps(state)}</script></main></body></html>"
    )


def _project(tmp_path: Path) -> Path:
    """Three plans, each earning a distinct-severity lifecycle row.

    The member sprint lists both ``mismatch`` and ``shipped_open``:
      - ``mismatch`` names the other sprint while the member sprint lists it,
        so the two sides of membership disagree and it reads as warn.
      - ``shipped_open`` is shipped (impl 1.0) while the open member sprint
        still lists it, so it reads as info.
      - ``no_impl`` is shipped with no impl and no sprint, so it earns no
        membership row and reads as error through MISSING_IMPL.
    """
    docs = tmp_path / PROJECT / "docs"
    (docs / ".reckon").mkdir(parents=True)
    (docs / ".reckon" / "project-state-migration.json").write_text(
        json.dumps(
            {
                "format": "distributed",
                "status": "complete",
                "project": PROJECT,
                "resources": [],
            }
        ),
        encoding="utf-8",
    )
    (docs / "sprints").mkdir()
    (docs / "sprints" / f"{MEMBER_SPRINT}.html").write_text(
        _sprint_html(
            MEMBER_SPRINT, status="active", members=["mismatch", "shipped_open"]
        ),
        encoding="utf-8",
    )
    (docs / "sprints" / f"{OTHER_SPRINT}.html").write_text(
        _sprint_html(OTHER_SPRINT, status="planned", members=[]),
        encoding="utf-8",
    )
    (docs / "mismatch.html").write_text(
        _plan_html("mismatch", sprint=OTHER_SPRINT), encoding="utf-8"
    )
    (docs / "shipped_open.html").write_text(
        _plan_html("shipped_open", sprint=MEMBER_SPRINT, status="shipped"),
        encoding="utf-8",
    )
    (docs / "no_impl.html").write_text(
        _plan_html("no_impl", sprint=None, status="shipped", impl="0"),
        encoding="utf-8",
    )
    return docs


def _lifecycle_rows(docs: Path):
    return doccheck.audit_lifecycle(project=PROJECT, docs_dir=docs)


def test_mcp_audit_reads_each_finding_own_severity(tmp_path: Path) -> None:
    docs = _project(tmp_path)
    root = docs.parent

    result = _audit(project=PROJECT, checkout_path=str(root))
    by_slug = {
        finding["slug"]: finding["severity"]
        for finding in result["findings"]
        if finding["code"] in {"SPRINT_MEMBERSHIP", "MISSING_IMPL"}
    }

    assert by_slug["shipped_open"] == "info"
    assert by_slug["mismatch"] == "warn"
    assert by_slug["no_impl"] == "error"


def test_cli_audit_table_prints_each_severity_and_exits_one(
    tmp_path: Path, monkeypatch
) -> None:
    docs = _project(tmp_path)
    rows = _lifecycle_rows(docs)

    monkeypatch.setattr(doccheck, "audit_lifecycle", lambda *, project: rows)

    result = CliRunner().invoke(main, ["audit", "--project", PROJECT, "--no-checks"])
    out = result.output

    by_slug = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[1] in {"mismatch", "shipped_open", "no_impl"}:
            by_slug[parts[1]] = parts

    assert "info" in by_slug["shipped_open"]
    assert "warn" in by_slug["mismatch"]
    assert "error" in by_slug["no_impl"]
    assert result.exit_code == 1
