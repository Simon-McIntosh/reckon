from __future__ import annotations

import json
import os
import time
from pathlib import Path

from click.testing import CliRunner

from reckon._plan_html import write_state
from reckon.cli import main


def _write_plan(docs_dir: Path, slug: str, state: dict) -> Path:
    bare = (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        f'<title>{slug}</title></head>'
        '<body><main class="plan-doc"></main></body></html>'
    )
    path = docs_dir / f"{slug}.html"
    path.write_text(write_state(bare, state), encoding="utf-8")
    return path


def _age_file(path: Path, *, days: int) -> None:
    stamp = time.time() - (days * 86400)
    os.utime(path, (stamp, stamp))


def test_audit_flags_stale_missing_impl_and_stale_rca(tmp_path, monkeypatch):
    project = "proj"
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps({project: str(docs_dir)}), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale = _write_plan(
        docs_dir,
        "stale-plan",
        {"slug": "stale-plan", "title": "Stale Plan", "status": "active", "impl": 0.5},
    )
    missing_impl = _write_plan(
        docs_dir,
        "missing-impl",
        {"slug": "missing-impl", "title": "Missing Impl", "status": "shipped"},
    )
    stale_rca = _write_plan(
        docs_dir,
        "stale-rca",
        {"slug": "stale-rca", "title": "Stale RCA", "type": "research", "status": "active"},
    )
    clean = _write_plan(
        docs_dir,
        "clean-plan",
        {"slug": "clean-plan", "title": "Clean Plan", "status": "done", "impl": 1.0},
    )

    _age_file(stale, days=31)
    _age_file(missing_impl, days=5)
    _age_file(stale_rca, days=61)
    _age_file(clean, days=3)

    result = CliRunner().invoke(main, ["audit"])

    assert result.exit_code == 1
    assert "project" in result.output
    assert "stale-plan" in result.output
    assert "STALE" in result.output
    assert "missing-impl" in result.output
    assert "MISSING_IMPL" in result.output
    assert "stale-rca" in result.output
    assert "STALE_RCA" in result.output
    assert "clean-plan" not in result.output


def test_audit_project_filter_limits_output(tmp_path, monkeypatch):
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(
        json.dumps({"proj-a": str(docs_a), "proj-b": str(docs_b)}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))

    stale_a = _write_plan(
        docs_a,
        "stale-a",
        {"slug": "stale-a", "title": "Stale A", "status": "active", "impl": 0.2},
    )
    stale_b = _write_plan(
        docs_b,
        "stale-b",
        {"slug": "stale-b", "title": "Stale B", "status": "active", "impl": 0.3},
    )
    _age_file(stale_a, days=31)
    _age_file(stale_b, days=31)

    result = CliRunner().invoke(main, ["audit", "--project", "proj-b"])

    assert result.exit_code == 0
    assert "stale-b" in result.output
    assert "proj-b" in result.output
    assert "stale-a" not in result.output
    assert "proj-a" not in result.output


def _open_followup(followup_id: str, **kwargs) -> dict:
    return {
        "id": followup_id,
        "status": "open",
        "recommends_skill": "",
        "prompt": "",
        "title": "Followup",
        "body": "Body",
        **kwargs,
    }


def _mounts(tmp_path, monkeypatch, mounts: dict) -> None:
    mounts_file = tmp_path / "mounts.json"
    mounts_file.write_text(json.dumps(mounts), encoding="utf-8")
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(mounts_file))


def test_audit_reports_followup_to_foreign_terminal_plan(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_TERMINAL,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup(
                    "f1",
                    recommends_skill="/reckon-build proj-b:foreign-terminal",
                    prompt="/reckon-build proj-b:foreign-terminal",
                )
            ],
        },
    )
    _write_plan(
        docs_b,
        "foreign-terminal",
        {
            "slug": "foreign-terminal",
            "title": "Shipped Elsewhere",
            "status": "shipped",
            "impl": 1.0,
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 1
    finding = followup_findings[0]
    assert finding.flag == FOLLOWUP_FOREIGN_TERMINAL
    assert finding.project == "proj-a"
    assert finding.slug == "holder"  # the holding plan
    assert finding.target_project == "proj-b"
    assert finding.target_slug == "foreign-terminal"  # the foreign target
    assert finding.target_status == "shipped"

    result = CliRunner().invoke(main, ["audit"])
    assert result.exit_code == 0
    assert "FOLLOWUP_FOREIGN_TERMINAL" in result.output
    assert "holder" in result.output
    assert result.output.count("FOLLOWUP_FOREIGN_TERMINAL") == 1


def test_audit_silent_for_followup_to_live_foreign_plan(tmp_path, monkeypatch):
    from reckon.doccheck import ForeignFollowupFinding, audit_lifecycle

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup("f1", recommends_skill="/reckon-build proj-b:live-plan")
            ],
        },
    )
    _write_plan(
        docs_b,
        "live-plan",
        {
            "slug": "live-plan",
            "title": "Live Elsewhere",
            "status": "active",
            "impl": 0.5,
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert followup_findings == []
    result = CliRunner().invoke(main, ["audit"])
    assert "FOLLOWUP_FOREIGN" not in result.output


def test_audit_reports_unmounted_foreign_target_distinctly(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_UNMOUNTED,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_a.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                _open_followup("f1", recommends_skill="/reckon-build proj-c:elsewhere")
            ],
        },
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 1
    finding = followup_findings[0]
    assert finding.flag == FOLLOWUP_FOREIGN_UNMOUNTED
    assert finding.target_project == "proj-c"
    assert finding.target_slug == "elsewhere"

    result = CliRunner().invoke(main, ["audit"])
    assert "FOLLOWUP_FOREIGN_UNMOUNTED" in result.output
    assert "FOLLOWUP_FOREIGN_TERMINAL" not in result.output


def test_audit_followup_finding_count_over_registered_mounts(tmp_path, monkeypatch):
    from reckon.doccheck import (
        FOLLOWUP_FOREIGN_TERMINAL,
        ForeignFollowupFinding,
        audit_lifecycle,
    )

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    for project, docs, holder_slug in (
        ("proj-a", docs_a, "holder-one"),
        ("proj-b", docs_b, "holder-two"),
    ):
        target_project = "proj-a" if project == "proj-b" else "proj-b"
        _write_plan(
            docs,
            holder_slug,
            {
                "slug": holder_slug,
                "title": holder_slug,
                "status": "active",
                "impl": 0.5,
                "followups": [
                    _open_followup(
                        f"f-{holder_slug}",
                        recommends_skill=f"/reckon-build {target_project}:terminator",
                    )
                ],
            },
        )
    for docs in (docs_a, docs_b):
        _write_plan(
            docs,
            "terminator",
            {"slug": "terminator", "title": "Done", "status": "done", "impl": 1.0},
        )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert len(followup_findings) == 2
    assert all(item.flag == FOLLOWUP_FOREIGN_TERMINAL for item in followup_findings)

    result = CliRunner().invoke(main, ["audit"])
    assert result.output.count("FOLLOWUP_FOREIGN") == 2


def test_audit_ignores_local_and_resolved_followup_refs(tmp_path, monkeypatch):
    from reckon.doccheck import ForeignFollowupFinding, audit_lifecycle

    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    docs_a.mkdir()
    docs_b.mkdir()
    _mounts(tmp_path, monkeypatch, {"proj-a": str(docs_a), "proj-b": str(docs_b)})
    _write_plan(
        docs_a,
        "holder",
        {
            "slug": "holder",
            "title": "Holder",
            "status": "active",
            "impl": 0.5,
            "followups": [
                # Local ref (no project qualifier) — never foreign.
                _open_followup("local", recommends_skill="/reckon-build a-local-plan"),
                # Resolved followup naming a foreign terminal plan — not pending.
                {
                    **_open_followup(
                        "resolved",
                        recommends_skill="/reckon-build proj-b:foreign-terminal",
                    ),
                    "resolved_at": "2026-09-01T00:00:00+00:00",
                    "resolved_by": "agent",
                    "outcome": "done",
                },
                # Same-project-qualified ref — reads as local, not foreign.
                _open_followup(
                    "self-qualified", recommends_skill="/reckon-build proj-a:holder"
                ),
            ],
        },
    )
    _write_plan(
        docs_b,
        "foreign-terminal",
        {"slug": "foreign-terminal", "title": "Done", "status": "done", "impl": 1.0},
    )

    followup_findings = [
        item for item in audit_lifecycle() if isinstance(item, ForeignFollowupFinding)
    ]
    assert followup_findings == []


# ─ audit-doc: the authoritative state, not the prose around it ──────────────
#
# The state reader walks every plan-* meta in document order and keeps the
# last, so a duplicated scalar is one value silently chosen rather than an
# ambiguity it reports; a typed resource carries its state in the
# reckon-resource-state island, so the island's absence is invisible to every
# presentation check; and a row's end tag is omittable in HTML, so an unclosed
# <tr> reflows the rest of the table into the last row's cell. Each specimen is
# built here rather than read from a live document: a check asserted against a
# live file turns red the day that file is legitimately repaired, which reads
# as a doccheck regression rather than as a finding.

_PLAN_SHELL = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="docs-project" content="proj">'
    "{head}"
    "<title>{slug}</title></head><body>"
    '<main class="plan-doc">{body}</main></body></html>'
)

# A plan that declares itself standalone carries no wiring finding, so each
# fixture below differs from a clean document only by the defect it asserts.
_STANDALONE = (
    '<meta name="plan-standalone" content="Fixture declares no wire; none is'
    ' asserted here">'
)


def _plan_doc(*, slug: str, head: str = "", body: str = "") -> str:
    return _PLAN_SHELL.format(slug=slug, head=head, body=body)


def _audit_doc(tmp_path: Path, name: str, text: str) -> tuple[int, str]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    result = CliRunner().invoke(main, ["audit-doc", str(path)])
    return result.exit_code, result.output


def _typed_sprint_island(island: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="docs-project" content="proj">'
        '<meta name="reckon-type" content="sprint">'
        '<meta name="reckon-id" content="S99">'
        '<meta name="reckon-version" content="3">'
        "<title>S99 | sprint</title></head><body>"
        '<main class="reckon-resource" data-type="sprint" data-id="S99">'
        '<ol data-reckon="sprint-items"></ol>'
        f"{island}"
        "</main></body></html>"
    )


def test_audit_doc_reports_duplicated_plan_scalar(tmp_path):
    # The reader keeps the last value, so the first line is dead state that a
    # writer can keep updating while every read reports the other value.
    text = _plan_doc(
        slug="dup-scalar",
        head=(
            _STANDALONE + '<meta name="plan-slug" content="dup-scalar">'
            '<meta name="plan-status" content="active">'
            '<meta name="plan-impl" content="0.9">'
            '<meta name="plan-impl" content="0.1">'
        ),
        body="<p>The body renders and the duplicate is silent.</p>",
    )
    code, output = _audit_doc(tmp_path, "dup-scalar.html", text)
    assert code != 0
    assert "duplicate-plan-scalar" in output
    assert "plan-impl" in output


def test_audit_doc_reports_missing_resource_state_island(tmp_path):
    # The island IS the resource's state; with it absent the document renders
    # and every presentation check passes while every plan read in the project
    # fails. No other meta or body content distinguishes this from a valid one.
    text = _typed_sprint_island("")
    code, output = _audit_doc(tmp_path, "island-missing.html", text)
    assert code != 0
    assert "resource-island-missing" in output


def test_audit_doc_reports_unparseable_resource_state_island(tmp_path):
    text = _typed_sprint_island(
        '<script type="application/json" id="reckon-resource-state">{not json</script>'
    )
    code, output = _audit_doc(tmp_path, "island-malformed.html", text)
    assert code != 0
    assert "resource-island-malformed" in output


def test_audit_doc_reports_unclosed_table_row(tmp_path):
    # Every row after the first is missing its end tag: the browser folds the
    # remaining rows into one cell, which no presentation check can see.
    text = _plan_doc(
        slug="unclosed-tr",
        head=(
            _STANDALONE + '<meta name="plan-slug" content="unclosed-tr">'
            '<meta name="plan-status" content="active">'
        ),
        body=("<table><tr><td>one</td></tr><tr><td>two</td><tr><td>three</td></table>"),
    )
    code, output = _audit_doc(tmp_path, "unclosed-tr.html", text)
    assert code != 0
    assert "tr-unclosed" in output


def test_audit_doc_clean_document_reports_ok(tmp_path):
    # The three checks above must not fire on a well-formed document: the
    # duplicate check reads every plan-* meta, so a document carrying one of
    # each beside a balanced table is the control the new checks stay silent on.
    text = _plan_doc(
        slug="clean",
        head=(
            _STANDALONE + '<meta name="plan-slug" content="clean">'
            '<meta name="plan-status" content="active">'
            '<meta name="plan-impl" content="1.0">'
        ),
        body=("<table><tr><td>one</td></tr><tr><td>two</td></tr></table>"),
    )
    code, output = _audit_doc(tmp_path, "clean.html", text)
    assert code == 0
    assert "OK" in output


def test_audit_doc_typed_resource_with_parseable_island_reports_ok(tmp_path):
    # The positive control for the island check: a well-formed island is the
    # case it must stay silent on, so the absence reports above are the check
    # firing rather than the check never running.
    text = _typed_sprint_island(
        '<script type="application/json" id="reckon-resource-state">'
        '{"id":"S99","type":"sprint","version":3}'
        "</script>"
    )
    code, output = _audit_doc(tmp_path, "island-present.html", text)
    assert code == 0
    assert "OK" in output
